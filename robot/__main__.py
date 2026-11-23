"""Run the robot.

    python -m robot check     is the AI server reachable? (config/robot.yaml, AI_HOST/AI_PORT)
    python -m robot sim       keyboard simulator with fake hardware, no AI server needed
    python -m robot scenarios scripted user scenarios run many times, with a report (docs/testing.md)
    python -m robot states    the state machine: --mermaid prints the diagram, --write regenerates docs/state_machine.md
    python -m robot run       the real robot (exits naming any driver that is not implemented yet)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m robot", description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check the AI server is reachable")
    check.add_argument("--host", help="override the configured host")
    check.add_argument("--port", type=int, help="override the configured port")
    sim = sub.add_parser("sim", help="keyboard simulator")
    sim.add_argument("--no-log", action="store_true", help="don't write a session log")
    sim.add_argument("--local-faces", action="store_true",
                     help="detect faces on the robot (OpenCV) instead of on the AI server, e.g. while the server's face "
                          "detection is down (needs --camera --faces)")
    sim.add_argument("--no-persist", action="store_true",
                     help="don't read or save the storage ring (data/state/storage.json): start empty, forget at the end")
    sim.add_argument("--llm", action="store_true",
                     help="explain/recommend through the AI server's LLM (default: catalogue text, no server needed)")
    sim.add_argument("--camera", action="store_true", help="the real webcam, and fail if there is none (from a terminal it is on by default)")
    sim.add_argument("--no-camera", action="store_true", help="the fake camera, no faces (the default when the input is piped)")
    sim.add_argument("--start", action="store_true",
                     help="start running: with faces on, the robot otherwise starts stopped, so it does not say hello as soon as it\n"
                          "starts; press Start on the operator page (or type `system start`)")
    sim.add_argument("--faces", action="store_true",
                     help="count the people the webcam sees with the AI server's face detection: a face makes the "
                          "robot say hello (needs --camera)")
    sim.add_argument("--auto-scan", action="store_true",
                     help="scan as soon as a book's barcode is held up to the camera, no need to type `book` "
                          "(needs --camera)")
    sim.add_argument("--preview", action="store_true",
                     help="also open a camera window on the robot (off by default: the operator page shows the camera)")
    sim.add_argument("--no-preview", action="store_true", help=argparse.SUPPRESS)      # the old flag; now the default
    sim.add_argument("--speak", action="store_true",
                     help="speak replies through the speaker (TTS from the AI server); the face display's mouth moves")
    sim.add_argument("--settle", type=float, default=120.0, metavar="SECONDS",
                     help="with piped input: after the last line, wait up to this long for the robot to finish its "
                          "goodbye and the trip putting books back (0 = quit at once)")
    sim.add_argument("--zone", choices=("quiet", "common"), help="start in this zone (default: zones.yaml default_zone, common); "
                     "`zone quiet` / `zone common` or the operator page changes it later")
    sim.add_argument("--live", action="store_true", help="--camera --faces --auto-scan --speak --llm together")
    sim.add_argument("--base", choices=("sim", "turtlebot", "fake"), default=None,
                     help="what drives the robot: sim (timers; the default, or navigation.backend), turtlebot (the Dell over the\n"
                          "cable, needs ROBOT_LINK_TOKEN; docs/robot_link.md) or fake (tools/fake_base.py started in this process)")
    sim.add_argument("--base-url", default=None, metavar="URL", help="with --base turtlebot: the Dell's address (default turtlebot.base_url)")
    sim.add_argument("--base-speed", type=float, default=1.0, metavar="TIMES", help="with --base fake: the fake base drives this many times faster than the style's speed limit allows (default %(default)s)")
    sim.add_argument("--config-set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                     help="override a setting of config/robot.yaml for this run (YAML value), e.g. returning.auto_start=true")
    film = sim.add_argument_group("film mode (the competition video: robot/film.py, docs/video_shot_list.md)")
    film.add_argument("--hold-zone", choices=("quiet", "common"), default=None,
                      help="hold this zone for the whole take, over the sound map, the noise and the operator")
    film.add_argument("--film", metavar="TAKE", default=None,
                      help="log timestamped events (greeting, replies, book read, goals, sound level...) to recordings/TAKE/events.jsonl")
    film.add_argument("--base-replay", metavar="FILE", default=None,
                      help="with --base fake: each drive replays a track recorded on the real robot (tools/video/trajectory.py), in real time")
    film.add_argument("--record", metavar="PATH", default=None,
                      help="write the camera picture with the faces boxed to this mp4 (needs --camera; the webcam is not opened twice)")
    states = sub.add_parser("states", help="print the state machine from its transition table")
    states.add_argument("--mermaid", action="store_true", help="print the Mermaid diagram")
    states.add_argument("--markdown", action="store_true", help="print the whole generated document")
    states.add_argument("--write", action="store_true", help="write docs/state_machine.md")
    scen = sub.add_parser("scenarios", help="scenario tests: list, run, report, replay, compare, live", add_help=False)
    scen.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("run", help="run on the real hardware")
    args = p.parse_args(argv)

    if args.command == "scenarios":
        from robot.scenarios.cli import main as scenarios_main
        return scenarios_main(args.rest)
    if args.command == "states":
        from robot.core.dialogue import SPEC
        from robot.core.state_doc import render_markdown, write_doc
        if args.write:
            print(f"wrote {write_doc()}")
        elif args.markdown:
            print(render_markdown(), end="")
        elif args.mermaid:
            print(SPEC.mermaid(grouped=True), end="")
        else:
            for name, state in SPEC.states.items():
                print(f"{SPEC.path(name):28} {'waits ' if state.waiting else '      '}{state.doc}")
        return 0

    from robot.config import load_config
    cfg = load_config()

    if args.command == "check":
        from tools.server_check import check as server_check
        ok, message = server_check(args.host or cfg.ai_host, args.port or cfg.ai_port)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    from robot.services.books import CatalogError
    if args.command == "sim":
        from robot.sim.keyboard import apply_overrides, run_sim
        try:
            apply_overrides(cfg, args.config_set)
        except ValueError as exc:
            print(f"--config-set: {exc}", file=sys.stderr)
            return 2
        try:
            # from a terminal the webcam and the faces are on by default (the robot starts stopped: Start on the operator page);
            # piped input (scripts, takes, tests) keeps the fake camera unless asked
            default_cam = sys.stdin.isatty() and not args.no_camera
            camera = (args.camera or args.live or default_cam) and not args.no_camera
            faces = (args.faces or args.live or default_cam) and camera
            return asyncio.run(run_sim(cfg, log_sessions=not args.no_log, use_llm=args.llm or args.live,
                                     use_camera=camera, speak=args.speak or args.live,
                                     use_faces=faces, preview=args.preview and not args.no_preview,
                                     start_stopped=faces and not args.start, camera_optional=camera and not (args.camera or args.live),
                                     auto_scan=args.auto_scan or args.live, settle_s=args.settle,
                                     persist_storage=not args.no_persist,
                                     local_faces=args.local_faces, initial_zone=args.zone, base=args.base, base_url=args.base_url,
                                     base_speed=args.base_speed, force_zone=args.hold_zone, film_take=args.film,
                                     record_path=args.record, base_replay=args.base_replay))
        except KeyboardInterrupt:
            # run_sim's cleanup has run. Exit without Python's shutdown, which can abort or hang while
            # the keyboard reader thread is still blocked in stdin.readline().
            sys.stdout.flush()
            os._exit(0)
        except CatalogError as exc:
            print(f"Cannot load the book catalogue: {exc}", file=sys.stderr)
            return 1

    from robot.app import build_app, real_drivers
    from robot.drivers import DriverNotImplemented
    try:
        drivers = real_drivers(cfg)
    except DriverNotImplemented as exc:
        print("Cannot run on the hardware yet. Missing driver(s):", file=sys.stderr)
        for missing in exc.missing:
            print(f"  - {missing}", file=sys.stderr)
        print("Use `python -m robot sim` to try the logic without hardware.", file=sys.stderr)
        return 2
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Cannot start a driver: {exc}", file=sys.stderr)
        return 2
    from robot.services.speech import Speech
    try:
        from robot.app import build_assistant
        assistant = build_assistant(cfg) if cfg.books["use_llm"] else None
        build_app(cfg, drivers, speech=Speech.from_config(cfg), assistant=assistant)
    except CatalogError as exc:
        print(f"Cannot load the book catalogue: {exc}", file=sys.stderr)
        return 1
    print("The real run loop (perception tasks) is not implemented yet.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
