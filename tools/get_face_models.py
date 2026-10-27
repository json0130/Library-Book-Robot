"""Download the face-embedding model used to tell visitors apart (person 1, person 2, ...).

    python tools/get_face_models.py

SFace (OpenCV Zoo, 38.7 MB) goes to models/face/, which is gitignored. The embeddings it makes stay in the
robot's memory for the session; nothing is sent to the AI server or written to disk.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
DEST = ROOT / "models" / "face" / "face_recognition_sface_2021dec.onnx"
MIN_BYTES = 30_000_000          # a failed download is a small HTML page, not a model


def main() -> int:
    if DEST.is_file() and DEST.stat().st_size >= MIN_BYTES:
        print(f"Already there: {DEST}")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEST.with_suffix(".part")
    print(f"Downloading {URL}")
    try:
        urllib.request.urlretrieve(URL, tmp)
        if tmp.stat().st_size < MIN_BYTES:
            raise OSError(f"only {tmp.stat().st_size} bytes came back")
        tmp.replace(DEST)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
