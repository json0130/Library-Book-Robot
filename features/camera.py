"""Live webcam face detection, recognition and emotion through the AI server.

    python main.py camera --host 10.42.0.118                  # live names (--recognition), q quits
    python main.py camera --host 10.42.0.118 --detect         # boxes only
    python main.py camera --host 10.42.0.118 --emotion        # live emotions
    python main.py camera --host 10.42.0.118 --enroll jay     # enroll 5 webcam photos

modules/face.py and modules/emotion.py are the server team's single-image clients
and stay unmodified; this file sends the same requests via modules.llm.request.
"""
from __future__ import annotations

import argparse
import base64
import os
import socket
import sys
import time

import cv2

from modules.llm import request

MAX_IMAGE_BYTES = 600_000
# mode -> (server command, request parameters)
MODES = {
    "detect": ("FACE", {"operation": "detect"}),
    "recognize": ("FACE", {"operation": "recognize"}),
    "emotion": ("EMOTION", {}),
}


def image_request(host: str, port: int, command: str, parameters: dict, jpg: bytes,
                  robot_id: str = socket.gethostname(), timeout: float = 30) -> dict:
    """Send one JPEG with a FACE or EMOTION command and return the response parameters.

    Raises OSError/EOFError on connection problems and ValueError on a bad or ERR response.
    """
    if not 0 < len(jpg) <= MAX_IMAGE_BYTES:
        raise ValueError("Image must be at most 600,000 bytes")
    return request(host, port, command, dict(parameters, format="jpeg"),
                   base64.b64encode(jpg).decode("ascii"), robot_id, timeout)["parameters"]


def label(d: dict) -> str:
    if "emotion" in d:
        return f"{d['emotion']} {d['confidence']:.0f}%"
    if "name" in d:
        return f"{d['name']} {d.get('similarity', 0):.2f}"
    return f"face {d['detection_confidence']:.2f}"


def open_camera(index: int):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise OSError(f"Could not open camera {index}")
    for _ in range(5):  # let auto exposure settle
        cap.read()
    return cap


def grab(cap):
    """Return (frame, jpeg_bytes)."""
    ok, frame = cap.read()
    if not ok:
        raise OSError("Could not read from the camera")
    return frame, cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def run_live(args) -> None:
    # ponytail: one blocking request per frame (detect ~50 ms, recognize ~120 ms, so 8-20 fps).
    # Move the request to a background thread if the video needs to stay smooth.
    cap = open_camera(args.camera)
    print("Press q in the video window to quit.", flush=True)
    try:
        while True:
            frame, jpg = grab(cap)
            t0 = time.perf_counter()
            command, parameters = MODES[args.mode]
            result = image_request(args.host, args.port, command, parameters, jpg, timeout=args.timeout)
            took = time.perf_counter() - t0
            detections = result.get("detections", [])
            print(f"{took:.2f}s  " + (" | ".join(label(d) for d in detections) or "no faces"), flush=True)
            for d in detections:
                top, right, bottom, left = d["box"]
                color = (0, 0, 255) if d.get("name") == "unknown" else (0, 200, 0)
                cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                cv2.putText(frame, label(d), (left, max(top - 8, 12)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            cv2.imshow("camera", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return
    finally:
        cap.release()
        cv2.destroyAllWindows()


def run_enroll(args) -> None:
    cap = open_camera(args.camera)
    print(f"Enrolling {args.count} photos as '{args.enroll}'. Look at the camera and turn your "
          "head a little between shots.", flush=True)
    done = 0
    try:
        while done < args.count:
            time.sleep(1)
            frame, jpg = grab(cap)
            cv2.imshow("enroll", frame)
            cv2.waitKey(1)
            detections = image_request(args.host, args.port, *MODES["detect"], jpg,
                                       timeout=args.timeout).get("detections", [])
            if not detections:
                print("  no face found, retrying", flush=True)
                continue
            # The server wants exactly one face, so crop to the largest (the person enrolling).
            top, right, bottom, left = max(
                (d["box"] for d in detections), key=lambda b: (b[2] - b[0]) * (b[1] - b[3]))
            m = (bottom - top) // 2  # margin so the server can re-detect the face in the crop
            h, w = frame.shape[:2]
            crop = frame[max(top - m, 0):min(bottom + m, h), max(left - m, 0):min(right + m, w)]
            crop_jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
            try:
                result = image_request(args.host, args.port, "FACE",
                                       {"operation": "enroll", "name": args.enroll}, crop_jpg,
                                       timeout=args.timeout)
            except ValueError as exc:  # e.g. the crop did not re-detect as exactly one face
                print(f"  rejected: {exc}", flush=True)
                continue
            done += 1
            dup = " (already enrolled)" if result.get("already_enrolled") else ""
            print(f"  enrolled {done}/{args.count}{dup}, server has {result.get('photo_count')} "
                  f"photos of {args.enroll}", flush=True)
    finally:
        cap.release()
        cv2.destroyAllWindows()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("AI_HOST", "10.42.0.118"),
                        help="AI server address (env: AI_HOST)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_PORT", 7898)),
                        help="AI server port (env: AI_PORT)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--camera", type=int, default=0, help="webcam index")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--recognition", dest="mode", action="store_const", const="recognize",
                      help="live boxes with enrolled names (default)")
    mode.add_argument("--detect", dest="mode", action="store_const", const="detect",
                      help="live face boxes only")
    mode.add_argument("--emotion", dest="mode", action="store_const", const="emotion",
                      help="live emotion per face")
    mode.add_argument("--enroll", metavar="NAME", help="enroll webcam photos as NAME")
    parser.add_argument("--count", type=int, default=5, help="photos to enroll")
    parser.set_defaults(mode="recognize")
    args = parser.parse_args(argv)
    try:
        run_enroll(args) if args.enroll else run_live(args)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, EOFError, ValueError) as exc:
        print(f"Camera request to {args.host}:{args.port} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
