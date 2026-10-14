"""Live check of "person 1, person 2...": the webcam with each face boxed and numbered.

    python tools/identity_test.py                  # q quits
    python tools/identity_test.py --threshold 0.5  # stricter: more likely to call a face a new person

Try: look at the camera (person 1), step out of view and back (still person 1), then let someone else step
in (person 2). The label shows each face's similarity to every person it has seen; the same person usually
scores well above the threshold and a different one well below. If one person keeps becoming a new number,
lower --threshold; if two people are merged, raise it. Then set perception.identity_threshold in
config/robot.yaml. Close the robot simulator first: only one program can use the webcam.

Needs the face model: python tools/get_face_models.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/identity_test.py`

from robot.config import load_config  # noqa: E402
from robot.drivers.camera import Camera  # noqa: E402
from robot.perception.identity import EmbedderUnavailable, FaceEmbedder, PersonTracker  # noqa: E402
from robot.services.vision import Vision  # noqa: E402


def main(argv=None) -> int:
    cfg = load_config()
    seen = cfg.robot.get("perception") or {}
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--host", default=cfg.ai_host)
    p.add_argument("--port", type=int, default=cfg.ai_port)
    p.add_argument("--camera", type=int, default=int(cfg.books["scanner_camera_index"]))
    p.add_argument("--threshold", type=float, default=float(seen.get("identity_threshold", 0.40)))
    p.add_argument("--interval", type=float, default=0.5, help="seconds between detections")
    args = p.parse_args(argv)
    try:
        import cv2
        embedder = FaceEmbedder(cfg.path(seen.get("face_model", "models/face/face_recognition_sface_2021dec.onnx")))
        camera = Camera(args.camera, cfg.robot.get("devices", {}).get("camera_width"),
                        cfg.robot.get("devices", {}).get("camera_height"), background=True)
    except (EmbedderUnavailable, OSError, RuntimeError) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 1
    tracker, vision = PersonTracker(embedder, args.threshold), Vision(args.host, args.port, timeout=10)
    print(f"Threshold {args.threshold}. Press q in the window to quit.", flush=True)
    boxes: list = []
    last, known = 0.0, 0
    try:
        while True:
            frame = camera.peek().copy()
            if time.monotonic() - last >= args.interval:
                last = time.monotonic()
                try:
                    detections = vision.detect(camera.encode_jpeg(frame))
                    people = tracker(frame, detections)
                except (OSError, EOFError, ValueError) as exc:
                    print(f"  detection failed: {exc}", flush=True)
                    detections, people = [], []
                boxes = [(d["box"], person, tracker.last_scores[i]) for i, (d, person) in enumerate(zip(detections, people))]
                if len(tracker.people) != known:
                    known = len(tracker.people)
                    print(f"  {known} people so far", flush=True)
                if boxes:
                    print("  " + " | ".join(f"{'person ' + str(pn) if pn else 'too small'}"
                                            + (" (" + ", ".join(f"{n}: {s:.2f}" for n, s in sc.items()) + ")" if sc else "")
                                            for _, pn, sc in boxes), flush=True)
            for (top, right, bottom, left), person, scores in boxes:
                cv2.rectangle(frame, (left, top), (right, bottom), (0, 200, 0), 2)
                text = f"person {person}" if person else "too small"
                cv2.putText(frame, text, (left, max(top - 24, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)
                if scores:
                    line = " ".join(f"p{n}:{s:.2f}" for n, s in scores.items())
                    cv2.putText(frame, line, (left, max(top - 6, 30)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
            cv2.imshow("identity test", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        camera.close()
        cv2.destroyAllWindows()
    print(f"Done: {len(tracker.people)} people seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
