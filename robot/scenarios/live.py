"""Guided mode for a human tester on the real robot (python -m robot scenarios live --scenario NAME).

The tester follows the scenario step by step. Each step is shown as a card in the terminal and on the /operator page
("Place a book on the pad and ask to return it"); only the tester presses `next` and `abort` (type them in the
terminal, or use the buttons on the /operator page). The robot is the real one: the camera, the display, the face
detection; what the tester does arrives as the usual events and is recorded with the robot's reactions in the same
format as the scripted runs (runs.jsonl and a transcript), then checked the same way.

No audio is recorded unless --record-audio is given: that saves a short clip of each `say` step to the run directory,
for team testing only, with the tester's agreement.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Optional

from robot.events import (BookPlaced, PersonEngaged, PersonLeft, PersonNearbyBusy, UserChoice, UserUtterance, BookDropped)
from robot.scenarios import checks as checks_module
from robot.scenarios import runner
from robot.scenarios.recorder import Recorder, RecordingAssistant
from robot.scenarios.spec import Scenario
from robot.scenarios.variants import choose_words

LIVE_ACTIONS = {PersonEngaged: ("engage", "a person engaged"), PersonNearbyBusy: ("busy", "a person is nearby but busy"),
                PersonLeft: ("leave", "the person left"), BookDropped: ("dropped", "the pad dropped the book"),
                BookPlaced: ("place_book", "a book was placed on the pad")}


def cards_for(scenario: Scenario, rng: random.Random, catalogue_books: list, typo_probability: float = 0.0,
              speech_probability: float = 0.0) -> tuple:
    """([card dicts], [choices]) for a scenario: what to tell the tester, with the random parts (branches, paraphrases,
    books) chosen from the seed. A card is {"n", "text", "kind", "wait_s"}."""
    cards, choices = [], []

    def add(kind, text, **extra):
        cards.append({"n": len(cards) + 1, "kind": kind, "text": text, **extra})

    def walk(steps):
        for step in steps:
            if isinstance(step, str):
                step = {step: None}
            ((kind, value),) = step.items()
            if kind == "one_of":
                branch = value[rng.randrange(len(value))]
                choices.append({"type": "branch", "value": branch["name"]})
                walk(branch["steps"])
            elif kind == "maybe":
                if rng.random() < value["p"]:
                    choices.append({"type": "maybe", "value": value["name"]})
                    walk(value["steps"])
            elif kind == "engage":
                add(kind, "Step in front of the robot, facing its camera, and wait for it to greet you.")
            elif kind == "busy":
                add(kind, "Walk past the robot without stopping or facing it (the robot should notice you).")
            elif kind == "leave":
                add(kind, "Walk away from the robot, out of its camera's view.")
            elif kind == "dropped":
                add(kind, "The pad's drop is simulated: wait a few seconds, or press next when the robot says it is stored.")
            elif kind == "start_returning":
                add(kind, "The operator starts the returning trip (it starts by itself in a moment).", auto="start_returning")
            elif kind == "place_book":
                isbn = value.get("isbn") if isinstance(value, dict) else value
                if isbn == "unknown":
                    add(kind, "Place a book that is NOT in the library catalogue on the pad (any other book with a barcode).")
                    choices.append({"type": "book", "value": "unknown ISBN"})
                else:
                    book = catalogue_books[rng.randrange(len(catalogue_books))] if isbn == "random" else None
                    name = book.title if book else str(isbn)
                    choices.append({"type": "book", "value": name})
                    add(kind, f"Place the book {name!r} (barcode up) on the pad.")
            elif kind == "say":
                options = value.get("text") if isinstance(value, dict) else value
                text, base, typed = choose_words(options, rng, typo_probability, speech_probability, title="it", author="the author")
                choices.append({"type": "paraphrase", "value": base, "variation": typed})
                add(kind, f"Ask the robot: \"{text}\"  (type: say {text})", say=text)
            elif kind == "tap":
                label = "one of the books in the list" if isinstance(value, dict) else f"the \"{value}\" button"
                add(kind, f"Touch {label} on the robot's screen (or type: pick ... on the terminal).")
            elif kind == "wait":
                if isinstance(value, dict):
                    add(kind, f"Wait and watch until the robot is in {value['until_state']} (up to {value['timeout']:g} s).")
                else:
                    add(kind, f"Wait {float(value):g} s.")
    walk(scenario.steps)
    return cards, choices


class LiveGuide:
    """Shows the cards one by one and waits for the tester's `next` / `abort`; records the run."""

    def __init__(self, scenario: Scenario, zone: str, seed: int, app, drivers, out_dir: Path, mode: str,
                 record_audio: bool = False, say=print):
        self.scenario, self.zone, self.seed, self.app, self.drivers = scenario, zone, seed, app, drivers
        self.out_dir, self.mode, self.record_audio, self.say = out_dir, mode, record_audio, say
        rng = random.Random(seed)
        self.cards, self.choices = cards_for(scenario, rng, app.finder.catalog.books,
                                             app.cfg.scenarios["typo_probability"], app.cfg.scenarios["speech_probability"])
        self.recorder = Recorder(time.monotonic)
        self.presses: asyncio.Queue = asyncio.Queue()          # "next" / "abort", pressed only by the tester
        self.aborted = False
        self.done = asyncio.Event()
        self.index = -1
        self.run_id = f"{scenario.name}-{zone}-live-{time.strftime('%H%M%S')}"
        self.record: Optional[dict] = None
        app.guide_card = None
        app.operator_extra = {"next": self.press_next, "abort": self.press_abort}
        self.recorder.attach(app.bus)
        self.recorder.wrap_actuators(app.actuators, drivers.esp32)
        app.bus.subscribe(object, self.on_event)
        self.started = time.time()
        self.stuck: list = []

    # ---- what the tester does arrives as events: record them as steps ----
    def on_event(self, event) -> None:
        for kind, (action, text) in LIVE_ACTIONS.items():
            if isinstance(event, kind):
                self.recorder.add("step", step=max(self.index, 0), action=action, detail=text)
                return
        if isinstance(event, UserUtterance):
            self.recorder.add("step", step=max(self.index, 0), action="say", detail=f"says: {event.text}",
                              text=event.text, paraphrase=event.text,
                              **{k: True for k in ("answerable", "unanswerable") if self._flag(k)})
        elif isinstance(event, UserChoice):
            self.recorder.add("step", step=max(self.index, 0), action="tap", detail=f"taps {event.choice!r}", choice=event.choice)

    def _flag(self, key: str) -> bool:
        return False                                  # a free-typed live question is not labelled answerable or not

    # ---- the tester's buttons ----
    async def press_next(self, arg: str = "") -> None:
        await self.presses.put("next")

    async def press_abort(self, arg: str = "") -> None:
        self.aborted = True
        await self.presses.put("abort")

    async def command(self, cmd: str) -> None:
        await (self.press_next() if cmd == "next" else self.press_abort())

    # ---- the guided run ----
    def show(self, card: dict) -> None:
        total = len(self.cards)
        self.app.guide_card = {"scenario": self.scenario.name, "n": card["n"], "total": total, "text": card["text"]}
        self.app.show_state()
        self.say(f"\n  +-- step {card['n']} of {total} " + "-" * 40)
        for part in card["text"].split("\n"):
            self.say(f"  |  {part}")
        self.say("  +-- type `next` when done, `abort` to stop (or use the buttons on the /operator page)")

    async def run(self) -> None:
        self.say(f"\n  Guided test: {self.scenario.name} ({self.zone} zone). {self.scenario.description}")
        if self.scenario.setup.get("stored"):
            self.app.dialogue.fill_storage(int(self.scenario.setup["stored"]))
        try:
            for card in self.cards:
                self.index = card["n"]
                self.recorder.add("step", step=card["n"], action="card", detail=f"card: {card['text'][:100]}")
                if card.get("auto") == "start_returning":
                    from robot.operator import Operator
                    await Operator(self.app).run("goto", "RETURNING")
                self.show(card)
                if self.record_audio and card["kind"] == "say":
                    await self.record_clip(card)
                await self.presses.get()
                if self.aborted:
                    self.say("  Aborted: nothing more is asked; the run is recorded as far as it went.")
                    break
            else:
                self.say("\n  All steps done. Wait for the robot to finish, then type `quit`.")
                await asyncio.sleep(0.5)
        finally:
            self.app.guide_card = None
            try:
                self.finish()
            except Exception as exc:                          # the tester must still get their terminal back
                import traceback
                self.say(f"  could not write the run record: {exc}\n{traceback.format_exc()}")
            self.done.set()

    async def record_clip(self, card: dict) -> None:
        """Opt-in (--record-audio): a short clip of what the tester says, for team testing."""
        audio_dir = self.out_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        path = audio_dir / f"{self.run_id}-step{card['n']}.wav"
        self.say(f"  [recording audio for 8 s -> {path}]")
        try:
            wav = await asyncio.get_running_loop().run_in_executor(None, self.drivers.audio.record, 8.0)
            path.write_bytes(wav)
        except (OSError, RuntimeError, AttributeError) as exc:
            self.say(f"  could not record audio ({exc}); carrying on without it")

    def finish(self) -> None:
        app, rec = self.app, self.recorder
        record = {"run_id": self.run_id, "scenario": self.scenario.name, "zone": self.zone, "seed": self.seed, "speed": 1.0,
                  "llm": self.mode, "started": self.started, "live": True, "aborted": self.aborted,
                  "tags": [c["value"] for c in self.choices if c["type"] == "branch"], "choices": self.choices,
                  "final_state": app.dialogue.state, "stuck": self.stuck, "error": "aborted by the tester" if self.aborted else None,
                  "storage": len(app.dialogue.storage), "catalogue_titles": [b.title for b in app.finder.catalog.books],
                  "timeline": rec.entries, "events": rec.event_names, "thresholds": app.cfg.scenarios["latency"],
                  "timeouts": dict(app.dialogue.settings.timeouts), "typo_probability": 0.0}
        record["checks"] = [c.to_json() for c in checks_module.run_checks(record, self.scenario)]
        record["passed"] = all(c["passed"] for c in record["checks"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with open(self.out_dir / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        runner.write_transcript(self.out_dir, record, self.scenario)
        self.record = record
        self.say(f"\n  Recorded {self.run_id}: automatic checks {'PASS' if record['passed'] else 'FAIL'}; "
                 f"transcript {runner.transcript_path(self.out_dir, self.run_id)}")


def live_command(args, scenarios: dict, cfg) -> int:
    from robot.scenarios.cli import pick_names
    from robot.sim.keyboard import run_sim
    (name,) = pick_names(scenarios, args.scenario)
    scenario = scenarios[name]
    if args.zone not in scenario.zones:
        raise ValueError(f"{name} does not run in the {args.zone} zone (zones: {', '.join(scenario.zones)})")
    out_dir = runner.new_run_dir()
    if args.record_audio:
        print("NOTE: --record-audio saves a short clip of each spoken step. For team testing only; tell the tester first.")
    guides: list = []

    def factory(app, sim, drivers):
        guide = LiveGuide(scenario, args.zone, args.seed, app, drivers, out_dir, args.llm, args.record_audio, sim.say)
        guides.append(guide)
        return guide

    code = asyncio.run(run_sim(cfg, sys.stdin, sys.stdout, log_sessions=False, use_llm=args.llm == "real",
                               use_camera=not args.no_camera, speak=not args.no_camera, use_faces=not args.no_camera,
                               preview=False, auto_scan=not args.no_camera, scenario_guide=factory,
                               initial_zone=args.zone, persist_storage=False))
    if guides and guides[0].record is not None:
        from robot.scenarios.report import write_report
        write_report(out_dir)
        print(f"Run directory: {out_dir}")
    return code
