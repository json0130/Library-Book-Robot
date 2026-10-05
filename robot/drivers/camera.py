"""Webcam frames as JPEG bytes. OpenCV is imported here only (on the Jetson it comes from apt:
sudo apt install python3-opencv)."""

from __future__ import annotations


class Camera:
    def __init__(self, index: int = 0):
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is not installed (sudo apt install python3-opencv)") from exc
        self._cv2 = cv2
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise OSError(f"Could not open camera {index}")
        for _ in range(5):          # let auto exposure settle
            self.cap.read()

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
