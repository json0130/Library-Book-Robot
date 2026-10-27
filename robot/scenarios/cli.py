"""python -m robot scenarios list | run | report | replay | compare | live"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from robot.config import load_config
from robot.scenarios import report as report_module
from robot.scenarios import runner
from robot.scenarios.checks import state_path
from robot.scenarios.spec import ScenarioError, ZONES, load_all


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m robot scenarios", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="the scenarios in scenarios/")
    run = sub.add_parser("run", help="run scenarios many times and write data/eval_runs/<timestamp>/")
    run.add_argument("--scenario", default="all", help="all or a scenario name (default all)")
    run.add_argument("--runs", type=int, default=10, help="runs per scenario, split evenly over the zones (default 10)")
    run.add_argument("--zones", default="quiet,common", help="comma-separated: quiet,common")
    run.add_argument("--llm", choices=("real", "mock"), default="mock",
                     help="real: the AI server in config/robot.yaml; mock: the repo's mock receiver (default)")
    run.add_argument("--seed", type=int, default=1, help="base seed; every run's own seed is derived from it and recorded")
    run.add_argument("--speed", type=float, default=10.0, help="run the robot's own timers this many times faster (default 10)")
    run.add_argument("--typos", type=float, default=None, help="chance that a `say` step gets typos (default scenarios.typo_probability)")
    run.add_argument("--speech", type=float, default=None,
                     help="chance that a `say` step gets speech-recognition style changes (default scenarios.speech_probability)")
    run.add_argument("--variants", type=Path, default=None, metavar="FILE",
                     help="held-out evaluation: say the phrasings in this YAML file (scenario -> step -> list) instead of the "
                          "scenarios' own; only the scenarios filled in are run (see scenarios/heldout_template.yaml)")
    run.add_argument("--out", type=Path, default=None, help="output directory (default data/eval_runs/<timestamp>)")
    rep = sub.add_parser("report", help="write summary.md and review.csv for a run directory")
    rep.add_argument("directory", type=Path)
    play = sub.add_parser("replay", help="run one recorded run again with the same seed and choices")
    play.add_argument("run_id")
    play.add_argument("--dir", type=Path, default=None, help="the run directory (default: the latest)")
    play.add_argument("--llm", choices=("real", "mock", "recorded"), default="recorded")
    play.add_argument("--variants", type=Path, default=None, metavar="FILE",
                      help="the phrasings file, for a run of a held-out batch")
    cmp_ = sub.add_parser("compare", help="compare two run directories")
    cmp_.add_argument("dir_a", type=Path)
    cmp_.add_argument("dir_b", type=Path)
    live = sub.add_parser("live", help="guided mode for a human tester on the real robot")
    live.add_argument("--scenario", required=True)
    live.add_argument("--zone", choices=ZONES, default="common")
    live.add_argument("--seed", type=int, default=1)
    live.add_argument("--llm", choices=("real", "mock", "none"), default="real")
    live.add_argument("--record-audio", action="store_true",
                      help="also record the microphone to the run directory (team testing only; off by default)")
    live.add_argument("--no-camera", action="store_true", help="use the fake camera (no webcam) for a dry run")
    return p


def pick_names(scenarios: dict, wanted: str) -> list:
    if wanted == "all":
        return list(scenarios)
    if wanted not in scenarios:
        raise ScenarioError(f"no scenario called {wanted!r}; scenarios: {', '.join(scenarios)}")
    return [wanted]


def llm_target(mode: str, cfg, stack: ExitStack) -> tuple:
    """(host, port, mode) for the LLM mode, starting the mock receiver or checking the real server."""
    if mode == "mock":
        host, port = stack.enter_context(runner.mock_llm_server())
        return host, port, "mock"
    host, port = runner.real_llm_target(cfg)
    return host, port, "real"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.ERROR, format="  %(message)s")
    try:
        return dispatch(args)
    except ScenarioError as exc:
        print(f"Scenario problem: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def dispatch(args) -> int:
    scenarios = load_all()
    if args.action == "list":
        width = max(len(n) for n in scenarios)
        for name, s in scenarios.items():
            print(f"{name:{width}}  zones: {','.join(s.zones):13} steps: {len(s.steps):2}  {s.description[:90]}")
        return 0
    if args.action == "report":
        summary, review = report_module.write_report(args.directory)
        print(summary.read_text(encoding="utf-8"))
        print(f"\nWrote {summary} and {review}")
        return 0
    if args.action == "compare":
        text = report_module.compare(args.dir_a, args.dir_b)
        print(text)
        target = Path(args.dir_b) / f"compare-vs-{Path(args.dir_a).name}.md"
        target.write_text(text, encoding="utf-8")
        print(f"Wrote {target}")
        return 0
    cfg = load_config()
    if args.action == "run":
        return run_command(args, scenarios, cfg)
    if args.action == "replay":
        return replay_command(args, scenarios, cfg)
    from robot.scenarios.live import live_command
    return live_command(args, scenarios, cfg)


def heldout_set(path: Path, scenarios: dict, wanted: str) -> dict:
    """The scenarios rewritten with the phrasings in `path`: only the ones filled in (a named --scenario must be one)."""
    from robot.scenarios.heldout import heldout_scenarios, load_variants
    chosen = heldout_scenarios(scenarios, load_variants(path, scenarios), path.name)
    if wanted != "all" and wanted not in chosen:
        raise ScenarioError(f"{path.name} has no phrasings for {wanted!r}; it has: {', '.join(chosen)}")
    return chosen if wanted == "all" else {wanted: chosen[wanted]}


def run_command(args, scenarios: dict, cfg) -> int:
    if args.variants is not None:
        scenarios = heldout_set(args.variants, scenarios, args.scenario)
    names = pick_names(scenarios, args.scenario)
    zones = [z.strip() for z in args.zones.split(",") if z.strip()]
    if not zones or any(z not in ZONES for z in zones):
        raise ValueError(f"--zones must be a comma-separated list of {', '.join(ZONES)}")
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")
    out_dir = args.out or runner.new_run_dir()
    plan = runner.plan_batch(names, scenarios, args.runs, zones, args.seed)
    if args.variants is not None:
        print(f"HELD-OUT batch: phrasings from {args.variants.name}, not the tuned paraphrases of the scenarios")
    print(f"{len(plan)} run(s): {', '.join(names)} | zones {','.join(zones)} | llm {args.llm} | seed {args.seed} | "
          f"speed x{args.speed:g}\nWriting to {out_dir}")
    with ExitStack() as stack:
        llm = llm_target(args.llm, cfg, stack)
        started = time.time()
        records = asyncio.run(runner.run_batch(plan, cfg, llm, args.speed, args.typos, out_dir,
                                                 speech_probability=args.speech))
    summary, review = report_module.write_report(out_dir)
    failed = [r for r in records if not r["passed"]]
    print(f"\n{len(records) - len(failed)}/{len(records)} runs passed in {time.time() - started:.0f} s.\n"
          f"Summary: {summary}\nReview sheet: {review}")
    return 0 if not failed else 1


def replay_command(args, scenarios: dict, cfg) -> int:
    directory = args.dir or runner.latest_run_dir()
    if directory is None:
        raise FileNotFoundError("no run directories in data/eval_runs/; pass --dir")
    records = {r["run_id"]: r for r in runner.load_runs(directory)}
    if args.run_id not in records:
        raise ValueError(f"{args.run_id} is not in {directory}; runs: {', '.join(list(records)[:6])}...")
    old = records[args.run_id]
    scenario = scenarios[old["scenario"]]
    if old.get("heldout"):
        if args.variants is None:
            raise ValueError(f"{args.run_id} is from a held-out batch ({old['heldout']}): pass --variants with that file")
        scenario = heldout_set(args.variants, scenarios, old["scenario"])[old["scenario"]]
    mode = old["llm"] if args.llm == "recorded" else args.llm
    with ExitStack() as stack:
        llm = llm_target(mode, cfg, stack) if mode in ("real", "mock") else ("none", None, "none")
        new = asyncio.run(runner.play(scenario, old["zone"], old["seed"], args.run_id, cfg, llm, old["speed"],
                                      old.get("typo_probability"), speech_probability=old.get("speech_probability", 0.0)))
    out_dir = runner.new_run_dir()
    out_dir.mkdir(parents=True)
    import json
    (out_dir / "runs.jsonl").write_text(json.dumps(new, ensure_ascii=False) + "\n", encoding="utf-8")
    runner.write_transcript(out_dir, new, scenario)
    same_choices = new["choices"] == old["choices"]
    same_path = state_path(new["timeline"]) == state_path(old["timeline"])
    print(f"Replayed {args.run_id} (seed {old['seed']}): result {'PASS' if new['passed'] else 'FAIL'} "
          f"(was {'PASS' if old['passed'] else 'FAIL'})")
    print(f"  same choices and wording: {'yes' if same_choices else 'NO'}")
    print(f"  same state path: {'yes' if same_path else 'no (timing or the LLM can differ)'}")
    print(f"  transcript: {runner.transcript_path(out_dir, args.run_id)}")
    return 0 if same_choices else 1
