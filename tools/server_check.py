"""Is the AI server reachable?

    python tools/server_check.py                   # host and port from config/robot.yaml (or AI_HOST/AI_PORT)
    python tools/server_check.py --host 172.24.47.255
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.services.llm import LLMClient, LLMError  # noqa: E402


def check(host: str, port: int) -> tuple[bool, str]:
    try:
        LLMClient(host, port).check()
    except LLMError as exc:
        return False, str(exc)
    return True, f"AI server reachable at {host}:{port}."


def main(argv=None) -> int:
    from robot.config import load_config
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--host", default=cfg.ai_host)
    p.add_argument("--port", type=int, default=cfg.ai_port)
    args = p.parse_args(argv)
    ok, message = check(args.host, args.port)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
