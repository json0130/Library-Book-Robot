"""Run the robot.

    python -m robot check     is the AI server reachable? (config/robot.yaml, AI_HOST/AI_PORT)
    python -m robot sim       keyboard simulator with fake hardware, no AI server needed
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
    sim.add_argument("--camera", action="store_true", help="scan with the real webcam instead of the fake camera")
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
    sim.add_argument("--live", action="store_true", help="--camera --faces --auto-scan --speak --llm together")
    states = sub.add_parser("states", help="print the state machine from its transition table")
    states.add_argument("--mermaid", action="store_true", help="print the Mermaid diagram")
    states.add_argument("--markdown", action="store_true", help="print the whole generated document")
    states.add_argument("--write", action="store_true", help="write docs/state_machine.md")
    sub.add_parser("run", help="run on the real hardware")
    args = p.parse_args(argv)

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
        from robot.sim.keyboard import run_sim
        try:
            return asyncio.run(run_sim(cfg, log_sessions=not args.no_log, use_llm=args.llm or args.live,
                                     use_camera=args.camera or args.live, speak=args.speak or args.live,
                                     use_faces=args.faces or args.live, preview=args.preview and not args.no_preview,
                                     auto_scan=args.auto_scan or args.live, settle_s=args.settle,
                                     persist_storage=not args.no_persist,
                                     local_faces=args.local_faces))
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
