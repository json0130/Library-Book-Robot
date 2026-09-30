#!/usr/bin/env python3
"""Start the library robot. For now each feature runs on its own:

    python main.py chat   [options]     talk to the LLM on the AI server
    python main.py camera [options]     live webcam: --recognition, --detect, --emotion, --enroll NAME

Add -h after a feature name for its options.
"""
import importlib
import sys

FEATURES = ("chat", "camera")  # each is features/<name>.py


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in FEATURES:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    # Imported on demand so one feature's dependencies (e.g. OpenCV) don't block the others.
    return importlib.import_module(f"features.{argv[0]}").main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
