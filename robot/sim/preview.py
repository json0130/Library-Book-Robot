"""A live window of what the robot's camera sees, with the detected faces boxed.

Runs on its own thread, so it keeps moving while the simulator waits on speech or the AI server.
It only peeks at the camera (Camera.peek), so it never takes frames from the barcode scanner.
Close the window or press q in it to stop the preview; the simulator carries on.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("robot.preview")

STALE_S = 2.5       # boxes older than this are not drawn: the face may have moved or gone


def display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Preview:
    def __init__(self, camera, counter=None, status: Optional[Callable[[], str]] = None,
                 title: str = "Library robot camera"):
        self.camera, self.counter, self.status, self.title = camera, counter, status, title
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="preview", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        import cv2
        shown = False
        try:
            while not self._stop.is_set():
                try:
                    frame = self.camera.peek().copy()
                except OSError:
                    time.sleep(0.1)
                    continue
                self._draw(cv2, frame)
                cv2.imshow(self.title, frame)
                shown = True
                if cv2.waitKey(30) & 0xFF == ord("q") or cv2.getWindowProperty(self.title, cv2.WND_PROP_VISIBLE) < 1:
                    break
        except Exception:                       # a broken GUI must never take the simulator down
            log.exception("camera preview stopped")
        finally:
            if shown:
                cv2.destroyWindow(self.title)
                cv2.waitKey(1)

    def _draw(self, cv2, frame) -> None:
        counter = self.counter
        if counter is not None and counter.detected_at is not None and time.monotonic() - counter.detected_at < STALE_S:
            for d in counter.detections:
                top, right, bottom, left = d["box"]
                cv2.rectangle(frame, (left, top), (right, bottom), (0, 200, 0), 2)
                label = f"person {d['person']}" if d.get("person") else f"face {d.get('detection_confidence', 0):.2f}"
                cv2.putText(frame, label, (left, max(top - 8, 14)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)
        if self.status is not None:
            text = self.status()
            cv2.putText(frame, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
            cv2.putText(frame, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
