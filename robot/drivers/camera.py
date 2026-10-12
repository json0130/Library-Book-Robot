"""Webcam frames as JPEG bytes. OpenCV is imported here only (on the Jetson it comes from apt:
sudo apt install python3-opencv)."""

from __future__ import annotations


import logging

log = logging.getLogger("robot.camera")


class Camera:
    """A webcam. width/height ask for a resolution (barcodes need more than OpenCV's 640x480
    default); the webcam only delivers it once the MJPG format is chosen first, so that is set
    whenever a size is requested. If the camera refuses, the size it gives is used and logged.
    width and height are the size of the frames it actually delivers."""

    def __init__(self, index: int = 0, width: int | None = None, height: int | None = None):
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

    def read_frame(self):
        """The next frame as a BGR numpy array."""
        ok, frame = self.cap.read()
        if not ok:
            raise OSError("Could not read from the camera")
        return frame

    def read_jpeg(self, quality: int = 85) -> bytes:
        ok, frame = self.cap.read()
        if not ok:
            raise OSError("Could not read from the camera")
        return self._cv2.imencode(".jpg", frame, [self._cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()

    def close(self) -> None:
        self.cap.release()
