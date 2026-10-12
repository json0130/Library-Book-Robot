"""Try the book barcode scanner without the robot.

    python tools/book_scan_test.py --list                  # the catalogue
    python tools/book_scan_test.py --image photo.jpg       # decode a photo, show the catalogue match
    python tools/book_scan_test.py --live                  # webcam with an overlay, q quits

Needs pyzbar and OpenCV: sudo apt install libzbar0 python3-opencv && pip install pyzbar
Test pictures: python tools/make_test_barcodes.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.app import build_finder  # noqa: E402
from robot.config import load_config  # noqa: E402
from robot.perception.book_scanner import DecoderUnavailable, decode_isbn  # noqa: E402
from robot.services.books import CatalogError  # noqa: E402


def describe(isbn: str, book) -> str:
    if book is None:
        return f"ISBN {isbn}: not in the catalogue and not found online"
    where = "catalogue" if book.found_in_catalog else "online (Open Library)"
    lines = [f"ISBN {isbn}: {book.title} by {book.author or 'unknown'}  [{where}]"]
    if book.found_in_catalog:
        lines.append(f"  shelf {book.shelf}, storage slot {book.slot}")
        lines.append(f"  {book.summary}")
    return "\n".join(lines)


def run_image(finder, path: Path) -> int:
    import cv2
    frame = cv2.imread(str(path))
    if frame is None:
        print(f"error: cannot read an image from {path}", file=sys.stderr)
        return 1
    isbn = decode_isbn(frame)
    if isbn is None:
        print("No ISBN barcode found.")
        return 1
    print(describe(isbn, finder.find(isbn)))
    return 0


def run_live(finder, camera_index: int, size: tuple | None = None) -> int:
    """Webcam view with the scanner running beside it.

    The picture is drawn at the camera's own rate from its newest frame; decoding happens on a
    second thread, on the newest frame whenever it is free, so a slow decode never holds the
    picture back or makes it lag.
    """
    import threading
    import time
    import cv2
    from robot.drivers.camera import Camera
    dev = load_config().robot.get("devices", {})
    width, height = size or (dev.get("camera_width"), dev.get("camera_height"))
    camera = Camera(camera_index, width, height, background=True)
    print(f"Camera {camera_index}: {camera.width}x{camera.height}")
    state = {"label": "looking for a barcode...", "last": None, "busy": False, "decode_ms": 0.0}

    def scan(frame):
        t0 = time.perf_counter()
        try:
            isbn = decode_isbn(frame)
            if isbn and isbn != state["last"]:
                state["last"] = isbn
                book = finder.find(isbn)
                print(describe(isbn, book), flush=True)
                state["label"] = f"{isbn}  {book.title if book else 'unknown book'}"
        except Exception as exc:                   # keep the view alive; say what went wrong
            state["label"] = f"scan error: {exc}"
        state["decode_ms"] = (time.perf_counter() - t0) * 1000
        state["busy"] = False

    print("Hold a book's barcode up to the camera. Press q in the window to quit.")
    shown, started = 0, time.perf_counter()
    try:
        while True:
            frame = camera.read_frame()
            if not state["busy"]:
                state["busy"] = True
                threading.Thread(target=scan, args=(frame.copy(),), daemon=True).start()
            shown += 1
            fps = shown / max(time.perf_counter() - started, 1e-6)
            view = frame.copy()
            cv2.putText(view, state["label"], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
            cv2.putText(view, f"{fps:.0f} fps, scan {state['decode_ms']:.0f} ms", (10, view.shape[0] - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imshow("book scanner", view)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return 0
            if shown == 60:                        # report the rate over a fresh window
                shown, started = 0, time.perf_counter()
    finally:
        camera.close()
        cv2.destroyAllWindows()


def parse_size(text: str) -> tuple:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"size must look like 640x480, got {text!r}") from None


def main(argv=None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", type=Path, help="decode this photo")
    mode.add_argument("--live", action="store_true", help="decode the webcam")
    mode.add_argument("--list", action="store_true", help="print the catalogue")
    p.add_argument("--camera", type=int, default=int(cfg.books["scanner_camera_index"]), help="webcam index")
    p.add_argument("--size", type=parse_size, metavar="WxH",
                   help="camera resolution for --live, e.g. 640x480 (smooth, 30 fps) or 1280x720 (sharper, "
                        "about 8 fps on this webcam); default from config/robot.yaml")
    args = p.parse_args(argv)
    try:
        finder = build_finder(cfg)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.list:
        for b in finder.catalog.books:
            print(f"{b.isbn13}  {b.title} ({b.author}) [{b.genre}] shelf {b.shelf}, slot {b.slot}")
        return 0
    try:
        return run_image(finder, args.image) if args.image else run_live(finder, args.camera, args.size)
    except DecoderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
