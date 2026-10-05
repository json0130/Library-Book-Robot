"""Run the robot.

    python -m robot check     is the AI server reachable? (config/robot.yaml, AI_HOST/AI_PORT)
    python -m robot sim       keyboard simulator with fake hardware, no AI server needed
    python -m robot run       the real robot (exits naming any driver that is not implemented yet)
"""

from __future__ import annotations

import argparse
import asyncio
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
    sub.add_parser("run", help="run on the real hardware")
    args = p.parse_args(argv)

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
            return asyncio.run(run_sim(cfg, log_sessions=not args.no_log))
        except KeyboardInterrupt:
            return 0
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
    voice = cfg.robot.get("voice", {}).get("tts_voice", "af_heart")
    try:
        build_app(cfg, drivers, speech=Speech(cfg.ai_host, cfg.ai_port, voice))
    except CatalogError as exc:
        print(f"Cannot load the book catalogue: {exc}", file=sys.stderr)
        return 1
    print("The real run loop (perception tasks) is not implemented yet.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
