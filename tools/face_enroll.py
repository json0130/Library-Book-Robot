"""Enroll a person's face on the AI server from webcam photos.

    python tools/face_enroll.py jay                 # 5 photos as "jay"
    python tools/face_enroll.py jay --count 8 --host 172.24.47.255

Same as `python tools/camera_test.py --enroll NAME`; other options are passed through.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/face_enroll.py`


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip(), file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    from tools.camera_test import main as camera_main
    return camera_main(["--enroll", argv[0], *argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
