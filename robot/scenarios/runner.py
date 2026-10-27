"""Running scenarios: build the real robot core on fake drivers, play a scenario's steps as a pretend visitor, record
everything (recorder.py), check it (checks.py) and write data/eval_runs/<timestamp>/runs.jsonl plus a transcript per run.

The robot is the real core (dialogue, screen composer, storage, floor plan) with the same events the simulator
publishes. Only its clock is scaled (`--speed`): the robot's timers run faster, while the LLM and the network keep their
real latency, which is measured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from robot.actuation.face import FaceDisplay
from robot.app import build_app, fake_drivers
from robot.config import ROOT, Config, load_config
from robot.events import (ContextChanged, PersonEngaged, PersonLeft, PersonNearbyBusy, BookDropped, BookPlaced,
                          UserChoice, UserUtterance)
from robot.operator import Operator
from robot.scenarios import checks as checks_module
from robot.scenarios.clock import ScaledClock
from robot.scenarios.recorder import Recorder, RecordingAssistant
from robot.scenarios.spec import Scenario, ZONES, load_all
from robot.scenarios.variants import choose_words, pick

UNKNOWN_ISBN = "9780140449136"            # a valid ISBN-13 that is not in the sample catalogue
RUNS_DIR = ROOT / "data" / "eval_runs"
log = logging.getLogger("robot.scenarios")


class RunAborted(Exception):
    """The tester pressed abort (live mode) or the run timed out."""


# ---------------------------------------------------------------------------------------------- LLM modes
@contextmanager
def mock_llm_server():
    """The repo's mock receiver (python -m vendor.ai_server.llm receive --mock) on a free local port; yields (host, port)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "vendor.ai_server.llm", "receive", "--mock", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("the mock LLM receiver did not start (python -m vendor.ai_server.llm receive --mock)")
                time.sleep(0.1)
        else:
            raise RuntimeError("the mock LLM receiver did not start listening")
        yield "127.0.0.1", port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def real_llm_target(cfg: Config) -> tuple:
    """(host, port) of the AI server, after the usual reachability check; raises SystemExit-worthy RuntimeError if down."""
    from tools.server_check import check
    ok, message = check(cfg.ai_host, cfg.ai_port)
    if not ok:
        raise RuntimeError(f"The AI server is not reachable, so `--llm real` cannot run.\n{message}\n"
                           "Use `--llm mock` (the repo's mock receiver) or fix the server first.")
    return cfg.ai_host, cfg.ai_port


# ---------------------------------------------------------------------------------------------- one run
@dataclass
class Context:
    """What the steps of one run share."""
    app: object
    recorder: Recorder
    clock: ScaledClock
    rng: random.Random
    zone: str
    cfg: dict
    operator: Operator
    tags: set = field(default_factory=set)
    choices: list = field(default_factory=list)
    last_book: Optional[object] = None
    step_no: int = 0
    typo_probability: float = 0.05
    speech_probability: float = 0.3
    stuck: list = field(default_factory=list)
    started_real: float = field(default_factory=time.monotonic)
    run_timeout_s: float = 240.0

    def check_time(self) -> None:
        if time.monotonic() - self.started_real > self.run_timeout_s:
            raise RunAborted(f"the run took more than {self.run_timeout_s:g} real seconds")


def build_robot(cfg: Config, assistant_factory, clock: ScaledClock, overrides: dict):
    """The real robot core on fake drivers (as the unit tests build it), with the scenario clock."""
    cfg.robot.setdefault("books", {})["online_fallback"] = False          # scenarios never touch the network for books
    drivers = fake_drivers()
    app = build_app(cfg, drivers, face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False)
    app.dialogue.assistant = assistant_factory(app) if assistant_factory else None
    app.dialogue.clock = clock
    app.screen.clock = clock
    for section, values in (overrides or {}).items():
        getattr(app.dialogue.settings, section).update(values)
    return app, drivers


async def settle(ctx: Context, think: bool = True) -> None:
    """Wait until the robot has finished what it is doing (speaking, scanning), then a short visitor think time."""
    app = ctx.app
    for _ in range(6000):
        ctx.check_time()
        busy = getattr(app.dialogue.fsm, "_running", False) or (app.scanner is not None and app.scanner.scanning)
        if not busy:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)
    if think:
        await ctx.clock.sleep(float(ctx.cfg.get("think_s", 0.8)))


def screen_targets(screen: Optional[dict]) -> list:
    """[(label, choice id)] for everything tappable on the current screen."""
    if not screen:
        return []
    view, payload = screen["view"], screen["payload"]
    targets = []
    if view == "options":
        targets += [(o["label"], o["id"]) for o in payload.get("options", [])]
    if view in ("book_card", "directions"):
        targets += [(b["label"], b["id"]) for b in payload.get("buttons", [])]
    if view == "book_list":
        targets += [(i["title"], f"book:{i['isbn']}") for i in payload.get("items", [])]
    targets += [(o["label"], o["id"]) for o in screen.get("options", [])]
    return targets


async def do_step(step, ctx: Context) -> None:
    """Perform one scenario step as the pretend visitor."""
    app, rec, bus = ctx.app, ctx.recorder, ctx.app.bus
    if isinstance(step, str):
        step = {step: None}
    ((kind, value),) = step.items()
    ctx.check_time()
    if kind == "one_of":
        branch = value[ctx.rng.randrange(len(value))]
        ctx.tags.add(branch["name"])
        ctx.choices.append({"step": ctx.step_no + 1, "type": "branch", "value": branch["name"]})
        rec.add("step", step=ctx.step_no + 1, action="branch", detail=branch["name"])
        for inner in branch["steps"]:
            await do_step(inner, ctx)
        return
    if kind == "maybe":
        taken = ctx.rng.random() < value["p"]
        ctx.choices.append({"step": ctx.step_no + 1, "type": "maybe", "value": f"{value['name']}={'yes' if taken else 'no'}"})
        if taken:
            ctx.tags.add(value["name"])
            rec.add("step", step=ctx.step_no + 1, action="branch", detail=value["name"])
            for inner in value["steps"]:
                await do_step(inner, ctx)
        return
    ctx.step_no += 1
    base = {"step": ctx.step_no, "action": kind}
    if kind == "engage":
        rec.add("step", **base, detail="a person engages")
        await bus.publish(PersonEngaged((1,), source="sensor"))
    elif kind == "busy":
        rec.add("step", **base, detail="a person is nearby but busy")
        await bus.publish(PersonNearbyBusy((1,), source="sensor"))
    elif kind == "leave":
        rec.add("step", **base, detail="the person walks away")
        await bus.publish(PersonLeft(source="sensor"))
    elif kind == "dropped":
        rec.add("step", **base, detail="the pad dropped the book")
        await bus.publish(BookDropped(source="sensor"))
    elif kind == "start_returning":
        rec.add("step", **base, detail="the operator starts a returning trip")
        await ctx.operator.run("goto", "RETURNING")
    elif kind == "place_book":
        await place_book(base, value, ctx)
    elif kind == "say":
        await say(base, value, ctx)
    elif kind == "tap":
        await tap(base, value, ctx)
    elif kind == "wait":
        await wait(base, value, ctx)
    await settle(ctx, think=kind not in ("wait", "start_returning"))


async def place_book(base: dict, value, ctx: Context) -> None:
    isbn = value.get("isbn") if isinstance(value, dict) else value
    catalogue = ctx.app.finder.catalog.books
    if isbn == "random":
        book = catalogue[ctx.rng.randrange(len(catalogue))]
        isbn = book.isbn13
        ctx.choices.append({"step": base["step"], "type": "book", "value": book.title})
    elif isbn == "unknown":
        ctx.tags.add("unknown_book")
        ctx.choices.append({"step": base["step"], "type": "book", "value": "unknown ISBN"})
        isbn, book = UNKNOWN_ISBN, None
    else:
        book = ctx.app.finder.catalog.lookup(isbn)
    ctx.last_book = book
    ctx.recorder.add("step", **base, detail=f"places a book on the pad: {book.title if book else 'unknown ISBN ' + isbn}",
                     isbn=isbn)
    camera = ctx.app.scanner_camera
    camera.show(isbn=isbn)
    await ctx.app.bus.publish(BookPlaced(source="sensor"))
    if ctx.app.scanner is not None:
        await ctx.app.scanner.wait()


async def say(base: dict, value, ctx: Context) -> None:
    options = value.get("text") if isinstance(value, dict) else value
    names = {"title": ctx.last_book.title if ctx.last_book else "it",
             "author": ctx.last_book.author if ctx.last_book else "the author"}
    text, chosen, typed = choose_words(options, ctx.rng, ctx.typo_probability, ctx.speech_probability, **names)
    ctx.choices.append({"step": base["step"], "type": "paraphrase", "value": chosen, "variation": typed})
    flags = {k: True for k in ("answerable", "unanswerable") if isinstance(value, dict) and value.get(k)}
    ctx.recorder.add("step", **base, detail=f"says: {text}", text=text, paraphrase=chosen, variation=typed, **flags)
    await ctx.app.bus.publish(UserUtterance(text, source="sensor"))


async def tap(base: dict, value, ctx: Context) -> None:
    screen = ctx.recorder.last_screen
    targets = screen_targets(screen)
    if isinstance(value, dict):                                   # {item: n | random}: a book in the list on the screen
        items = [t for t in targets if t[1].startswith("book:")]
        if not items:
            ctx.recorder.add("step", **base, detail="taps a book in the list", error="no list of books on the screen")
            return
        index = ctx.rng.randrange(len(items)) if value["item"] == "random" else min(int(value["item"]), len(items) - 1)
        ctx.choices.append({"step": base["step"], "type": "list item", "value": items[index][0]})
        label, choice = items[index]
    else:
        wanted = value.strip().lower()
        match = next(((l, c) for l, c in targets if l.lower() == wanted), None) or \
            next(((l, c) for l, c in targets if wanted in l.lower()), None)
        if match is None:
            shown = ", ".join(l for l, _ in targets) or "nothing tappable"
            ctx.recorder.add("step", **base, detail=f"taps {value!r}", error=f"no button {value!r} on the screen "
                             f"(view {screen['view'] if screen else 'none'}; tappable: {shown})")
            return
        label, choice = match
    ctx.recorder.add("step", **base, detail=f"taps {label!r}", choice=choice)
    await ctx.app.bus.publish(UserChoice(choice, source="screen"))


async def wait(base: dict, value, ctx: Context) -> None:
    if isinstance(value, dict):
        target, timeout = value["until_state"], float(value["timeout"])
        ctx.recorder.add("step", **base, detail=f"waits until {target} (at most {timeout:g} s)")
        deadline = ctx.clock() + timeout
        while ctx.clock() < deadline:
            ctx.check_time()
            state = ctx.app.dialogue.state
            if state == target or state.endswith("." + target):
                return
            await asyncio.sleep(0.01)
        message = f"still in {ctx.app.dialogue.state} after {timeout:g} s: never reached {target}"
        ctx.stuck.append(message)
        ctx.recorder.add("error", where="wait", message=message)
    else:
        ctx.recorder.add("step", **base, detail=f"waits {float(value):g} s")
        end = ctx.clock() + float(value)
        while ctx.clock() < end:
            ctx.check_time()
            await asyncio.sleep(0.01)


async def play(scenario: Scenario, zone: str, seed: int, run_id: str, cfg: Config, llm: tuple, speed: float,
               typo_probability: Optional[float] = None, steps=None, guide=None,
               speech_probability: Optional[float] = None) -> dict:
    """Run one scenario once in one zone. llm: ("none", None) | (host, port, 'real'|'mock'). Returns the run record."""
    harness = cfg.scenarios
    clock = ScaledClock(speed)
    rng = random.Random(seed)
    recorder = Recorder(clock)
    handler = recorder.log_handler()
    root = logging.getLogger("robot")
    root.addHandler(handler)
    started = time.time()
    factory = None
    if llm and llm[0] != "none":
        from robot.services.llm import LLMClient
        timeout = float(cfg.robot["ai_server"].get("llm_timeout_s", 20))
        factory = lambda app: RecordingAssistant(LLMClient(llm[0], llm[1], timeout), recorder)
    app, drivers = build_robot(cfg, factory, clock, harness.get("robot_overrides"))
    recorder.attach(app.bus)
    recorder.wrap_actuators(app.actuators, drivers.esp32)
    ctx = Context(app, recorder, clock, rng, zone, harness, Operator(app), typo_probability=(
        harness["typo_probability"] if typo_probability is None else typo_probability),
        speech_probability=harness["speech_probability"] if speech_probability is None else speech_probability,
        run_timeout_s=float(harness["run_timeout_s"]))
    error = None

    async def ticker():
        while True:
            await app.dialogue.tick()
            await app.screen.tick()
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    try:
        await app.bus.publish(ContextChanged(zone, 1))
        if scenario.setup.get("stored"):
            app.dialogue.fill_storage(int(scenario.setup["stored"]))
            recorder.add("step", step=0, action="setup", detail=f"{len(app.dialogue.storage)} book(s) already in storage")
        await settle(ctx, think=False)
        for step in (steps if steps is not None else scenario.steps):
            await do_step(step, ctx)
        await settle(ctx, think=False)
    except RunAborted as exc:
        error = str(exc)
        recorder.add("error", where="run", message=error)
    except Exception as exc:                                         # a bug in the robot or the harness: record, don't hide
        log.exception("run %s failed", run_id)
        error = f"{type(exc).__name__}: {exc}"
        recorder.add("error", where="run", message=error, exception=True)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        root.removeHandler(handler)
    for event, handler_, exc in app.bus.errors:                       # the bus isolates handler failures: they are still bugs
        recorder.add("error", where=f"handler for {type(event).__name__}", message=f"{type(exc).__name__}: {exc}", exception=True)
    record = {"run_id": run_id, "scenario": scenario.name, "zone": zone, "seed": seed, "speed": speed,
              "llm": llm[2] if llm and len(llm) > 2 else "none", "started": started, "tags": sorted(ctx.tags),
              "choices": ctx.choices, "final_state": app.dialogue.state, "stuck": ctx.stuck, "error": error,
              "storage": len(app.dialogue.storage), "catalogue_titles": [b.title for b in app.finder.catalog.books],
              "timeline": recorder.entries, "events": recorder.event_names,
              "thresholds": harness["latency"], "timeouts": dict(app.dialogue.settings.timeouts),
              "typo_probability": ctx.typo_probability, "speech_probability": ctx.speech_probability,
              "heldout": scenario.heldout}
    record["checks"] = [c.to_json() for c in checks_module.run_checks(record, scenario)]
    record["passed"] = all(c["passed"] for c in record["checks"])
    return record


# ---------------------------------------------------------------------------------------------- batches
def seed_for(base: int, scenario: str, zone: str, index: int) -> int:
    """A different but reproducible seed for every run of a batch."""
    return (base * 1_000_003 + sum(ord(c) * (i + 7) for i, c in enumerate(f"{scenario}/{zone}")) * 7919 + index * 104729) % (2 ** 31)


def zone_split(runs: int, zones: list) -> list:
    """Run counts per zone, as even as possible (10 runs over 2 zones = 5 + 5; the odd run goes to the first zone)."""
    base, extra = divmod(runs, len(zones))
    return [base + (1 if i < extra else 0) for i in range(len(zones))]


def plan_batch(names: list, scenarios: dict, runs: int, zones: list, seed: int) -> list:
    """[(scenario, zone, seed, run_id)] for the whole batch."""
    plan = []
    for name in names:
        scenario = scenarios[name]
        usable = [z for z in zones if z in scenario.zones]
        for zone, count in zip(usable, zone_split(runs, usable)):
            for i in range(count):
                plan.append((scenario, zone, seed_for(seed, name, zone, i), f"{name}-{zone}-{i + 1:03d}"))
    return plan


async def run_batch(plan: list, cfg: Config, llm: tuple, speed: float, typo_probability: Optional[float], out_dir: Path,
                    progress=print, speech_probability: Optional[float] = None) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    with open(out_dir / "runs.jsonl", "w", encoding="utf-8") as f:
        for scenario, zone, seed, run_id in plan:
            record = await play(scenario, zone, seed, run_id, cfg, llm, speed, typo_probability,
                                  speech_probability=speech_probability)
            records.append(record)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            write_transcript(out_dir, record, scenario)
            failed = [c["name"] for c in record["checks"] if not c["passed"]]
            progress(f"  {run_id:34} seed {seed:<11} {'PASS' if record['passed'] else 'FAIL ' + ', '.join(failed[:3])}")
    (out_dir / "meta.json").write_text(json.dumps({"llm": llm[2] if len(llm) > 2 else "none", "speed": speed,
                                                   "runs": len(records), "created": time.time(),
                                                   "heldout": next((r["heldout"] for r in records if r.get("heldout")), "")}), encoding="utf-8")
    return records


def transcript_path(out_dir: Path, run_id: str) -> Path:
    return Path(out_dir) / "transcripts" / f"{run_id}.md"


def write_transcript(out_dir: Path, record: dict, scenario: Scenario) -> Path:
    from robot.scenarios.report import render_transcript
    path = transcript_path(out_dir, record["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_transcript(record, scenario), encoding="utf-8")
    return path


def new_run_dir() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / stamp
    n = 1
    while path.exists():
        n += 1
        path = RUNS_DIR / f"{stamp}-{n}"
    return path


def latest_run_dir() -> Optional[Path]:
    dirs = sorted(p for p in RUNS_DIR.glob("*") if (p / "runs.jsonl").is_file()) if RUNS_DIR.is_dir() else []
    return dirs[-1] if dirs else None


def load_runs(directory) -> list:
    path = Path(directory) / "runs.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: is that a scenario run directory?")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
