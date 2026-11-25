"""Face detection on the robot itself (OpenCV's frontal-face Haar cascade), for when the AI server's face detection is
down or not wanted. It has the same `detect(jpg)` as services/vision.Vision, so PeopleCounter takes either.

It finds frontal faces only and is less accurate than the server's model (it may miss a turned or tilted head, or
box a poster), but it needs no network and no download. Use it with `python -m robot sim --local-faces` or
`perception.detector: local` in config/robot.yaml.
"""

from __future__ import annotations

import os
from typing import Optional

CASCADE = "haarcascade_frontalface_default.xml"
SEARCH = ("/usr/share/opencv4/haarcascades", "/usr/share/opencv/haarcascades",
          "/usr/local/share/opencv4/haarcascades", "/usr/local/share/opencv/haarcascades")


class LocalFacesUnavailable(RuntimeError):
    pass


def find_cascade(cv2=None) -> Optional[str]:
    dirs = list(SEARCH)
    data = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if data:
        dirs.insert(0, data)
    return next((os.path.join(d, CASCADE) for d in dirs if os.path.isfile(os.path.join(d, CASCADE))), None)


class LocalFaceDetector:
    def __init__(self, min_face_px: int = 40, scale: float = 1.1, neighbors: int = 5):
        try:
            import cv2
            import numpy
        except ImportError as exc:
            raise LocalFacesUnavailable(f"OpenCV is not installed ({exc}); sudo apt install python3-opencv") from exc
        path = find_cascade(cv2)
        if path is None:
            raise LocalFacesUnavailable(f"{CASCADE} not found; look in /usr/share/opencv4/haarcascades")
        self.cv2, self.np = cv2, numpy
        self.cascade = cv2.CascadeClassifier(path)
        if self.cascade.empty():
            raise LocalFacesUnavailable(f"could not load {path}")
        self.min_face_px, self.scale, self.neighbors = min_face_px, scale, neighbors

    def detect(self, jpg: bytes) -> list[dict]:
        """[{"box": [top, right, bottom, left], "detection_confidence": ...}] like the server's answer."""
        image = self.cv2.imdecode(self.np.frombuffer(jpg, self.np.uint8), self.cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("not a decodable image")
        image = self.cv2.equalizeHist(image)
        found = self.cascade.detectMultiScale(image, self.scale, self.neighbors,
                                              minSize=(self.min_face_px, self.min_face_px))
        return [{"box": [int(y), int(x + w), int(y + h), int(x)], "detection_confidence": 0.9}
                for x, y, w, h in found]
