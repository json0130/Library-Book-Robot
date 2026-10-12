"""Webcam frames as JPEG bytes. OpenCV is imported here only (on the Jetson it comes from apt:
sudo apt install python3-opencv)."""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("robot.camera")


class Camera:
    """A webcam. width/height ask for a resolution (barcodes need more than OpenCV's 640x480
    default); the webcam only delivers it once the MJPG format is chosen first, so that is set
    whenever a size is requested. If the camera refuses, the size it gives is used and logged.
    width and height are the size of the frames it actually delivers.

    background=True reads the camera on its own thread and keeps only the newest frame. Without
    it, frames queue up inside the driver whenever the caller is slower than the camera (for
    example while decoding a barcode), so what the caller gets runs seconds behind. With it,
    read_frame() always returns the newest frame, waiting for one that is newer than the last
    one it returned. Call close() to stop the thread.

    Mind the camera's own limits: this webcam gives 30 fps at 640x480 but only about 8 fps at
    1280x720, whatever the caller does."""

    def __init__(self, index: int = 0, width: int | None = None, height: int | None = None,
                 background: bool = False):
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is not installed (sudo apt install python3-opencv)") from exc
        self._cv2 = cv2
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise OSError(f"Could not open camera {index}")
        if width and height:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        frame = None
        for _ in range(5):          # let auto exposure settle
            ok, frame = self.cap.read()
        # The size comes from a real frame: right after set(), the camera's own size query can
        # still report the old size.
        self.height, self.width = (frame.shape[:2] if ok and frame is not None else (0, 0))
        if width and height and (self.width, self.height) != (int(width), int(height)):
            log.warning("camera %s gave %dx%d instead of the requested %sx%s",
                        index, self.width, self.height, width, height)
        self.last_frame_time = time.monotonic()      # when the newest frame was captured
        self._cond = threading.Condition()
        self._latest, self._seq, self._served, self._error = None, 0, 0, None
        self._stop = False
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._grab_loop, name=f"camera-{index}", daemon=True)
            self._thread.start()

    def _grab_loop(self) -> None:
        while not self._stop:
            ok, frame = self.cap.read()
            with self._cond:
                if ok:
                    self._latest, self._error = frame, None
                    self._seq += 1
                    self.last_frame_time = time.monotonic()
                else:
                    self._error = OSError("Could not read from the camera")
                self._cond.notify_all()
            if not ok:
                time.sleep(0.05)

    def read_frame(self, timeout: float = 2.0):
        """The next frame as a BGR numpy array. With background=True, the newest frame that has
        not been returned yet (waiting up to `timeout` seconds for one)."""
        if self._thread is None:
            ok, frame = self.cap.read()
            if not ok:
                raise OSError("Could not read from the camera")
            return frame
        with self._cond:
            self._cond.wait_for(lambda: self._seq > self._served or self._error is not None, timeout)
            if self._seq == self._served:
                raise self._error or OSError(f"No frame from the camera within {timeout:g} s")
            self._served = self._seq
            return self._latest

    def read_jpeg(self, quality: int = 85) -> bytes:
        frame = self.read_frame()
        return self._cv2.imencode(".jpg", frame, [self._cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()

    def close(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.cap.release()
