"""Where the robot is, live, for the operator page's floor plan: a read-only look at the Dell's /api/state, twice a second.

The display server runs this next to the page so the robot's position is on the floor plan whether or not the robot program is running (a
survey, teaching, a drive from a script). It only READS: GET /api/state sends no heartbeat, no goal and no speed, so it cannot make the base
move and does not keep the base "awake" (the Dell still stops 3 s after the robot program's heartbeats stop).

Needs the ssh tunnel (scripts/robot_link_tunnel.sh robot@<the Dell> 8010) and ROBOT_LINK_TOKEN in the environment (or in a mode-600 file named by
ROBOT_LINK_TOKEN_FILE). The token is never printed, logged or sent anywhere but that header. Without it the page says so and shows nothing.

    python -m display.server                 # picks it up when ROBOT_LINK_TOKEN is set; ROBOT_LINK_URL changes http://127.0.0.1:8010
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:8010"
TRAIL_POINTS = 400            # about the last few minutes of driving
TRAIL_STEP_M = 0.05           # a new trail point only after the robot moved this far


def token_from_env(env=None) -> str:
    """The shared secret: ROBOT_LINK_TOKEN, else the first line of the file ROBOT_LINK_TOKEN_FILE names if only its owner can read it."""
    env = os.environ if env is None else env
    token = (env.get("ROBOT_LINK_TOKEN") or "").strip()
    if token:
        return token
    path = (env.get("ROBOT_LINK_TOKEN_FILE") or "").strip()
    if path:
        try:
            if os.stat(path).st_mode & 0o077:
                return ""                                     # readable by others: not used
            with open(path, encoding="utf-8") as f:
                return f.readline().strip()
        except OSError:
            return ""
    return ""


class PoseFeed:
    def __init__(self, base_url: str = DEFAULT_URL, token: str = "", interval_s: float = 0.5, timeout_s: float = 1.0, clock=time.time, fetch=None):
        self.base_url, self.token, self.interval_s, self.timeout_s, self.clock = base_url.rstrip("/"), token, interval_s, timeout_s, clock
        self._fetch = fetch or self._http_get
        self.lock = threading.Lock()
        self.latest: Optional[dict] = None
        self.seen_at: Optional[float] = None
        self.error = "" if token else "ROBOT_LINK_TOKEN is not set where the display server was started, so the position cannot be read"
        self.trail: list = []
        self._stop = threading.Event()

    # ---------------------------------------------------------------- reading
    def _http_get(self) -> dict:
        req = urllib.request.Request(self.base_url + "/api/state", headers={"X-Robot-Token": self.token}, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))

    def poll_once(self) -> None:
        if not self.token:
            return
        try:
            state = self._fetch()
        except urllib.error.HTTPError as exc:
            self._fail("the base refused the request (HTTP %d): is ROBOT_LINK_TOKEN the one the Dell uses?" % exc.code)
            return
        except (OSError, ValueError) as exc:
            self._fail("cannot reach the base at %s (%s): is the tunnel up?" % (self.base_url, exc.__class__.__name__))
            return
        pose = state.get("pose") if isinstance(state, dict) else None
        if not isinstance(pose, dict) or not all(isinstance(pose.get(k), (int, float)) for k in ("x", "y", "yaw")):
            self._fail("the base sent no pose")
            return
        with self.lock:
            self.latest, self.seen_at, self.error = state, self.clock(), ""
            if not self.trail or math.hypot(pose["x"] - self.trail[-1][0], pose["y"] - self.trail[-1][1]) >= TRAIL_STEP_M:
                self.trail.append([round(pose["x"], 2), round(pose["y"], 2)])
                del self.trail[:-TRAIL_POINTS]

    def _fail(self, why: str) -> None:
        with self.lock:
            self.error = why

    def clear_trail(self) -> None:
        with self.lock:
            self.trail = []

    # ---------------------------------------------------------------- what the page gets
    def snapshot(self) -> dict:
        with self.lock:
            age = None if self.seen_at is None else round(self.clock() - self.seen_at, 1)
            if self.latest is None or self.error or age is None or age > 5:
                return {"available": False, "reason": self.error or "no position from the base yet", "trail": list(self.trail)}
            state = self.latest
            nav = state.get("nav") or {}
            return {"available": True, "age_s": age, "pose": state["pose"], "nav": {k: nav.get(k) for k in ("status", "distance_left", "reason")},
                    "trail": list(self.trail)}

    # ---------------------------------------------------------------- the thread
    def start(self) -> "PoseFeed":
        threading.Thread(target=self._run, daemon=True, name="pose-feed").start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval_s)
