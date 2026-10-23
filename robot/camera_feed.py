"""Sends the robot's camera picture to the display server so the operator page can show it (/camera.mjpg).

The camera belongs to the robot process and the web pages to the display server, so the robot posts a small JPEG
a few times a second (`POST /camera`, only accepted from the robot's own computer) and the server streams the newest
one to whoever has the operator page open. Faces the people counter has found are boxed, with their person number.
If the display server is not running it tries again every few seconds, quietly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("robot.camera_feed")
RETRY_S = 5.0


def draw_faces(cv2, frame, detections) -> None:
    """Box each detected face ({"box": [top, right, bottom, left], "person": n}) on the frame."""
    for d in detections or []:
        top, right, bottom, left = d["box"]
        cv2.rectangle(frame, (left, top), (right, bottom), (45, 235, 235), 2)
        label = f"person {d['person']}" if d.get("person") else "face"
        cv2.putText(frame, label, (left, max(12, top - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (45, 235, 235), 1)


class CameraFeed:
    """fps: pictures per second while someone has the operator page open (the server says so in its reply); with
    nobody watching it sends one a second, so the Jetson does no encoding for nothing. width: the picture is
    shrunk to this many pixels wide (the page shows it small), which makes it quick to encode and send."""

    IDLE_S = 1.0

    def __init__(self, camera, counter=None, host: str = "127.0.0.1", port: int = 8765, fps: float = 15.0,
                 width: int = 480, quality: int = 60):
        self.camera, self.counter = camera, counter
        self.host, self.port, self.interval = host, port, 1.0 / max(1.0, fps)
        self.width, self.quality = width, quality
        self.viewers = 1                            # assume someone is watching until the server says otherwise
        self._last = None                           # the last frame sent, so a frame is not sent twice

    def jpeg(self) -> Optional[bytes]:
        """The newest picture as a small JPEG (blocking: run in a thread), with the faces boxed when there are any;
        None if the camera has no new picture since the last one."""
        cam = self.camera
        if not (hasattr(cam, "peek") and hasattr(cam, "encode_jpeg")):
            peek = getattr(cam, "peek_jpeg", None)
            return peek(self.quality) if peek else None
        frame = cam.peek()
        if frame is self._last:
            return None
        self._last = frame
        try:
            import cv2
        except ImportError:
            return cam.encode_jpeg(frame, self.quality)
        detections = getattr(self.counter, "detections", None)
        if detections:                                           # box the faces at full size, then shrink
            frame = frame.copy()
            draw_faces(cv2, frame, detections)
        height, width = frame.shape[:2]
        if width > self.width:
            frame = cv2.resize(frame, (self.width, round(height * self.width / width)), interpolation=cv2.INTER_AREA)
        return cam.encode_jpeg(frame, self.quality)

    def post(self, jpg: bytes) -> bool:
        req = urllib.request.Request(f"http://{self.host}:{self.port}/camera", data=jpg,
                                     headers={"Content-Type": "image/jpeg"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                try:
                    self.viewers = int(json.loads(resp.read()).get("viewers", 1))
                except (ValueError, TypeError):
                    self.viewers = 1
                return True
        except (urllib.error.URLError, OSError):
            return False

    async def step(self) -> bool:
        """Send one picture if there is a new one; False if the display server could not be reached."""
        loop = asyncio.get_running_loop()
        try:
            jpg = await loop.run_in_executor(None, self.jpeg)
        except (OSError, EOFError, ValueError, RuntimeError) as exc:
            log.debug("camera feed: no picture (%s)", exc)
            return True
        if not jpg:
            return True
        return await loop.run_in_executor(None, self.post, jpg)

    async def run(self) -> None:
        while True:
            ok = await self.step()
            await asyncio.sleep(RETRY_S if not ok else self.interval if self.viewers > 0 else self.IDLE_S)
