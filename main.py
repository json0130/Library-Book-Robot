#!/usr/bin/env python3
"""Forwards to `python -m robot` (check, sim, run). The old test features moved to tools/:

    python main.py chat    ->  python tools/chat_test.py
    python main.py camera  ->  python tools/camera_test.py   (enrolling: tools/face_enroll.py)
    python main.py display ->  python -m display.server
"""
import sys

MOVED = {"chat": "python tools/chat_test.py", "camera": "python tools/camera_test.py",
         "display": "python -m display.server"}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in MOVED:
        print(f"'{argv[0]}' moved: run {MOVED[argv[0]]} {' '.join(argv[1:])}".rstrip(), file=sys.stderr)
        return 2
    print("Note: test tools now live in tools/ (chat_test, camera_test, face_enroll, server_check).",
          file=sys.stderr)
    from robot.__main__ import main as robot_main
    return robot_main(argv)


if __name__ == "__main__":
    sys.exit(main())
