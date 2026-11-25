"""Brings the base's map to the operator page: the Dell's occupancy map (GET /api/map) -> the display server (POST /map) ->
the page (/map.png, /map.json), where it is drawn with the robot on it and a click picks where to go.

The robot process owns the link and the web pages belong to the display server, so this asks the base for the map every few
seconds (only a changed map comes back: the link says which version it already has) and hands a new one to the display server.
It also asks the display server which version it holds, so a display server that was restarted gets the map again. If either
side is away it tries again at the next turn, quietly.

StatePush keeps the robot's dot on that map moving: the operator page only learns where the base is when the robot sends its
state, which otherwise happens on screen changes, so it sends it again whenever the pose, the link or the drive has changed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("robot.map_feed")


class MapFeed:
    def __init__(self, link, host: str = "127.0.0.1", port: int = 8765, every_s: float = 5.0):
        self.link, self.host, self.port, self.every_s = link, host, port, float(every_s)
        self.latest: Optional[dict] = None          # the newest map from the base (with its "png")

    def _shown_version(self) -> Optional[int]:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/map.json", timeout=2) as resp:
                return json.loads(resp.read()).get("version")
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            return None

    def _post(self, image: dict) -> bool:
        body = {k: image[k] for k in ("version", "resolution", "width", "height", "origin", "frame")}
        body["png_base64"] = base64.b64encode(image["png"]).decode("ascii")
        req = urllib.request.Request(f"http://{self.host}:{self.port}/map", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def step(self) -> bool:
        """One turn (blocking: run it in a thread). True if a map was handed to the display server."""
        got = self.link.fetch_map()
        if got and got.get("changed") and "png" in got:
            self.latest = got
            log.info("[map] %dx%d cells at %.2f m, version %s", got["width"], got["height"], got["resolution"], got["version"])
        if self.latest is None:
            return False
        if self._shown_version() == self.latest["version"]:
            return False
        return self._post(self.latest)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(None, self.step)
            except Exception:                                    # the map page must never take the robot down
                log.exception("map feed")
            await asyncio.sleep(self.every_s)


class StatePush:
    """Sends the state (with the base's pose) to the operator page whenever the pose, the link or the drive changed."""

    def __init__(self, app, every_s: float = 1.0):
        self.app, self.every_s = app, float(every_s)
        self.sent: Optional[tuple] = None

    @staticmethod
    def signature(link: dict) -> tuple:
        pose, nav = link.get("pose") or {}, link.get("nav") or {}
        left = nav.get("distance_left")
        return (round(pose.get("x", 0.0) or 0.0, 2), round(pose.get("y", 0.0) or 0.0, 2), round(pose.get("yaw", 0.0) or 0.0, 2),
                link.get("status"), link.get("localised"), link.get("battery"), nav.get("status"), nav.get("goal_id"),
                None if left is None else round(left, 1))

    def pending(self) -> Optional[tuple]:
        """(state info, its signature) when something changed since the last one that was sent, else None."""
        info = self.app.state_info()
        sig = self.signature(info.get("link") or {})
        return None if sig == self.sent else (info, sig)

    def send(self, info: dict, sig: tuple) -> bool:
        """Blocking: run it in a thread. The change counts as sent only when the display server took it."""
        if self.app.actuators.face.show_state(info):
            self.sent = sig
            return True
        return False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                due = self.pending()
                if due is not None:
                    await loop.run_in_executor(None, self.send, *due)
            except Exception:                                    # the dot on the map must never take the robot down
                log.exception("state push")
            await asyncio.sleep(self.every_s)
