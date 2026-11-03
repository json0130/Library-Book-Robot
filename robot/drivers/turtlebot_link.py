"""The Jetson's side of the link to the Dell on the TurtleBot (the contract is docs/robot_link.md, api_version 1).

Standard library only (urllib, threading), and it never blocks the asyncio loop: three background threads do the talking.

    heartbeat   POST /api/heartbeat every second. Its own thread, because it is what keeps the base driving: the base
                stops by itself after 3 s without one, so nothing else may ever delay it.
    poller      GET /api/state twice a second and GET /api/health every few seconds; turns what it reads into events.
    sender      the commands (goal, cancel, speaking, max_speed) one after another, in order. These are idempotent (a
                goal carries its own goal_id), so a failed one is tried again, up to `retries` times.

Events go on the robot's bus: LinkUp, LinkDown, PoseUpdated, NavProgress, NavArrived, NavFailed, NavCancelled, NoiseReading.
The link status is *connected* (the last request worked), *degraded* (some requests failed lately) or *lost* (nothing
answered for `lost_after_s`; entering it publishes LinkDown, the first reply after it LinkUp).

A goal's outcome is read from `nav` of the base's state, for the goal_id that was sent, and published once. A goal the
base never confirmed is sent again (same id) until it is accepted, replaced, cancelled or the link is lost (then it is
dropped: the base has stopped, and the dialogue sends a new goal when the link is back).

The methods that talk (`heartbeat_once`, `poll_once`, `health_once`, `run_command`) are plain functions with an injectable
HTTP client and clock, so the tests drive them without threads.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import itertools
import json
import logging
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from robot.events import (LinkDown, LinkUp, NavArrived, NavCancelled, NavFailed, NavProgress, NoiseReading,
                          PoseUpdated)

log = logging.getLogger("robot.link")

API_VERSION = 1
TOKEN_ENV = "ROBOT_LINK_TOKEN"
POSE_MOVED_M = 0.05                 # a PoseUpdated when the base moved this far...
POSE_TURNED_RAD = 0.05              # ...or turned this much
POSE_EVERY_S = 5.0                  # ...or at least this often
PROGRESS_STEP_M = 0.05              # a NavProgress when the distance left changed by this much
RESEND_EVERY_S = 1.0                # an unconfirmed goal is sent again this often
CLOCK_SKEW_WARN_S = 1.0
MAP_TIMEOUT_S = 5.0                 # GET /api/map carries a picture: allowed longer than the 1 s of a state poll
TRANSIENT_REASONS = ("no_heartbeat",)      # a refusal that goes away by itself: tried again


class LinkError(Exception):
    """The base could not be reached, or answered something that is not the contract (a timeout, a refused connection, bad JSON)."""


class Unreachable(LinkError):
    """No connection at all (refused, no route, timed out): the Dell is off, the cable or Wi-Fi is down, or its address changed."""


class ApiError(LinkError):
    """The base answered with an error status (docs/robot_link.md section 1). `status` is the HTTP status, `error` its code."""

    def __init__(self, status: int, error: str = "", detail: str = ""):
        self.status, self.error, self.detail = status, error, detail
        super().__init__(f"HTTP {status} {error} {detail}".strip())


class LinkConfigError(ValueError):
    """The link cannot be set up (no ROBOT_LINK_TOKEN, say)."""


@dataclass
class LinkSettings:
    base_url: str = "http://192.168.50.2:8010"
    token: str = ""
    timeout_s: float = 1.0
    poll_hz: float = 2.0
    heartbeat_hz: float = 1.0
    health_every_s: float = 5.0
    lost_after_s: float = 3.0
    retries: int = 2

    @classmethod
    def from_config(cls, cfg, env: Optional[dict] = None, base_url: Optional[str] = None) -> "LinkSettings":
        """From config/robot.yaml `turtlebot:`; the token only from the environment (ROBOT_LINK_TOKEN)."""
        env = os.environ if env is None else env
        token = (env.get(TOKEN_ENV) or "").strip()
        if not token:
            raise LinkConfigError(f"{TOKEN_ENV} is not set: it is the shared secret of the link to the base "
                                  "(docs/robot_link.md, section 5). It is never kept in the config file.")
        t = cfg.turtlebot
        return cls(base_url=(base_url or t["base_url"]).rstrip("/"), token=token, timeout_s=float(t["timeout_s"]),
                   poll_hz=float(t["poll_hz"]), heartbeat_hz=float(t["heartbeat_hz"]),
                   health_every_s=float(t["health_every_s"]), lost_after_s=float(t["lost_after_s"]),
                   retries=int(t["retries"]))


class Http:
    """One JSON request at a time to the base. Raises LinkError (ApiError for an error status)."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 1.0):
        self.base_url, self.token, self.timeout_s = base_url.rstrip("/"), token, float(timeout_s)

    def request(self, method: str, path: str, body: Optional[dict] = None, timeout_s: Optional[float] = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"X-Robot-Token": self.token, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                err = {}
            raise ApiError(exc.code, str(err.get("error", "")), str(err.get("detail", ""))) from None
        except (urllib.error.URLError, OSError) as exc:                           # refused, no route, timed out, reset
            raise Unreachable(f"base unreachable at {self.base_url}: {getattr(exc, 'reason', exc)}") from None
        except (ValueError, http.client.HTTPException) as exc:                    # a malformed reply
            raise LinkError(str(exc)) from None
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            raise LinkError("the reply is not JSON") from None
        if not isinstance(parsed, dict):
            raise LinkError("the reply is not a JSON object")
        return parsed

    def get(self, path: str, timeout_s: Optional[float] = None) -> dict:
        return self.request("GET", path, None, timeout_s)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)


@dataclass
class Goal:
    """The one goal this client is following."""
    goal_id: str
    x: float
    y: float
    yaw: float
    accepted_at: Optional[float] = None         # our clock, when the base said yes (None: not confirmed yet)
    sending: bool = False                       # a send is queued or in progress
    last_try: float = 0.0
    last_distance: Optional[float] = None
    cancelling: bool = False


@dataclass
class Command:
    kind: str                                   # goal | cancel | speaking | max_speed
    body: dict = field(default_factory=dict)


def number(value, default=None):
    """A finite float, or the default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


class TurtleBotLink:
    def __init__(self, settings: LinkSettings, http: Optional[Http] = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.http = http or Http(settings.base_url, settings.token, settings.timeout_s)
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.sink: Callable[[object], None] = lambda event: None      # where events go (attach() sets the bus)
        self.lock = threading.RLock()
        self.last_ok: Optional[float] = None
        self.failures = 0                       # failed requests since the last good one
        self.unreachable = False                # the last failure was "no connection" (the Dell's address may have changed)
        self.incompatible = ""                  # the base's api_version is not ours: the reason, else ""
        self._up = False                        # the last of LinkUp / LinkDown published was LinkUp
        self.pose: dict = {}                    # the base's latest pose, nav and noise, as it sent them
        self.nav: dict = {}
        self.noise: Optional[dict] = None
        self.ros_ok: Optional[bool] = None
        self.localised: Optional[bool] = None
        self.battery: Optional[float] = None
        self.clock_skew_s: Optional[float] = None
        self._goal: Optional[Goal] = None
        self._last_pose_sent: Optional[tuple] = None        # (x, y, yaw, localised, monotonic time)
        self._last_noise_t: Optional[float] = None
        self._speaking = False
        self._max_speed: Optional[float] = None
        self._max_speed_supported = True
        self._map_version: Optional[int] = None             # the map version the operator page already has
        self._commands: "queue.Queue[Optional[Command]]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list = []
        self._counter = itertools.count(1)
        self._prefix = format(int(wall() * 1000) % (36 ** 4), "x")
        self._pump: Optional[asyncio.Task] = None

    # ---- ids and status ----
    def new_goal_id(self) -> str:
        return f"g-{self._prefix}-{next(self._counter)}"

    @property
    def status(self) -> str:
        """connected | degraded | lost."""
        with self.lock:
            if self.last_ok is None or self.incompatible or self.clock() - self.last_ok >= self.settings.lost_after_s:
                return "lost"
            return "degraded" if self.failures else "connected"

    @property
    def goal_id(self) -> Optional[str]:
        goal = self._goal
        return goal.goal_id if goal else None

    def available(self) -> bool:
        """Can a goal be sent and driven now: the link works, ROS is up on the base and it knows where it is."""
        return self.status != "lost" and self.ros_ok is not False and self.localised is not False

    def _emit(self, event) -> None:
        try:
            self.sink(event)
        except Exception:                                # a broken listener must not take the link down
            log.exception("could not deliver %s", type(event).__name__)

    # ---- bookkeeping of replies ----
    def _ok(self) -> None:
        with self.lock:
            self.last_ok, self.failures, self.unreachable = self.clock(), 0, False
        self.evaluate()

    def _failed(self, why) -> None:
        with self.lock:
            self.unreachable = isinstance(why, Unreachable)
            self.failures += 1
            first = self.failures == 1
        if first:
            log.info("[link] request failed: %s", why)
        self.evaluate()

    def evaluate(self) -> str:
        """Publish LinkDown / LinkUp when the status has crossed into lost / out of lost. Also called on a timer: a link that
        goes quiet produces no replies to notice."""
        status = self.status
        with self.lock:
            went_down = status == "lost" and self._up
            came_up = status != "lost" and not self._up
            if went_down:
                self._up = False
                self._goal = None                            # the base has stopped by itself; the dialogue sends a new goal
            elif came_up:
                self._up = True
        if went_down:
            reason = self.incompatible or f"no reply for {self.settings.lost_after_s:g} s"
            if self.unreachable and not self.incompatible:
                reason += (f" - base unreachable at {self.settings.base_url}: check that the ssh tunnel is running "
                           "(scripts/robot_link_tunnel.sh) or the Dell's IP (turtlebot.base_url), and that robot_link_api is running there")
            log.warning("[link] LOST (%s)", reason)
            self._emit(LinkDown(reason))
        elif came_up:
            log.info("[link] UP: %s", self.settings.base_url)
            self.enqueue(Command("cancel", {"goal_id": "*"}))            # a goal left from before a crash or a cable pull
            if self._speaking:
                self.enqueue(Command("speaking", {"speaking": True}))
            if self._max_speed is not None and self._max_speed_supported:
                self.enqueue(Command("max_speed", {"m_s": self._max_speed}))
            self._emit(LinkUp(self.settings.base_url))
        return status

    # ---- talking ----
    def heartbeat_once(self) -> bool:
        try:
            self.http.post("/api/heartbeat", {"t": round(self.wall(), 3)})
        except LinkError as exc:
            self._failed(exc)
            return False
        self._ok()
        return True

    def health_once(self) -> Optional[dict]:
        try:
            health = self.http.get("/api/health")
        except LinkError as exc:
            self._failed(exc)
            return None
        version = health.get("api_version")
        with self.lock:
            self.ros_ok = health.get("ros_ok") if isinstance(health.get("ros_ok"), bool) else None
            if isinstance(health.get("localised"), bool):
                self.localised = health["localised"]
            self.battery = number(health.get("battery"))
            now = number(health.get("time"))
            self.clock_skew_s = None if now is None else round(now - self.wall(), 3)
            self.incompatible = "" if version == API_VERSION else f"the base speaks api_version {version}, this robot {API_VERSION}"
        if self.clock_skew_s is not None and abs(self.clock_skew_s) > CLOCK_SKEW_WARN_S:
            log.warning("[link] the base's clock is %.1f s off ours (chrony? docs/robot_link.md section 5)", self.clock_skew_s)
        self._ok()
        return health

    def poll_once(self) -> Optional[dict]:
        requested = self.clock()
        try:
            state = self.http.get("/api/state")
        except LinkError as exc:
            self._failed(exc)
            return None
        pose, nav, noise = state.get("pose"), state.get("nav"), state.get("noise")
        if not isinstance(pose, dict) or not isinstance(nav, dict):
            self._failed("the state has no pose or nav")
            return None
        self._ok()
        self._read_pose(pose)
        self._read_noise(noise)
        self._read_nav(nav, requested)
        return state

    def fetch_state(self) -> dict:
        """The base's state, once, for tools (raises LinkError). Does not publish anything."""
        return self.http.get("/api/state")

    def _read_pose(self, pose: dict) -> None:
        x, y, yaw = number(pose.get("x")), number(pose.get("y")), number(pose.get("yaw"))
        localised = bool(pose.get("localised"))
        if x is None or y is None or yaw is None:
            return
        with self.lock:
            self.pose = dict(pose)
            self.localised = localised
            last, now = self._last_pose_sent, self.clock()
            moved = (last is None or localised != last[3] or math.hypot(x - last[0], y - last[1]) >= POSE_MOVED_M
                     or abs(math.atan2(math.sin(yaw - last[2]), math.cos(yaw - last[2]))) >= POSE_TURNED_RAD
                     or now - last[4] >= POSE_EVERY_S)
            if moved:
                self._last_pose_sent = (x, y, yaw, localised, now)
        if moved:
            self._emit(PoseUpdated(x, y, yaw, localised, str(pose.get("frame") or "map")))

    def _read_noise(self, noise) -> None:
        with self.lock:
            self.noise = dict(noise) if isinstance(noise, dict) else None
        if not isinstance(noise, dict):
            return
        overall, t = number(noise.get("overall")), number(noise.get("t"), 0.0)
        if overall is None or t == self._last_noise_t:
            return
        self._last_noise_t = t
        self._emit(NoiseReading(overall, number(noise.get("left")), number(noise.get("right")), number(noise.get("balance")),
                                str(noise.get("unit") or "dBFS"), bool(noise.get("calibrated")),
                                bool(noise.get("robot_noise")), t))

    def _read_nav(self, nav: dict, requested: float) -> None:
        with self.lock:
            self.nav = dict(nav)
            goal = self._goal
        if goal is None:
            return
        status, gid, reason = nav.get("status"), nav.get("goal_id"), str(nav.get("reason") or "")
        if gid != goal.goal_id:
            # the base shows another goal (or none) although it said yes to ours: it lost it. A poll that was asked for
            # before the base answered "accepted" may still show the old state: not that.
            if goal.accepted_at is not None and requested > goal.accepted_at:
                self._finish(goal)
                log.warning("[link] the base no longer has goal %s (it shows %s)", goal.goal_id, gid or "none")
                self._emit(NavFailed(goal.goal_id, "goal_lost"))
            return
        if status == "active":
            distance = number(nav.get("distance_left"))
            if goal.last_distance is None or distance is None or abs((distance or 0) - goal.last_distance) >= PROGRESS_STEP_M:
                goal.last_distance = distance if distance is not None else goal.last_distance
                self._emit(NavProgress(goal.goal_id, distance))
        elif status == "succeeded":
            self._finish(goal)
            self._emit(NavArrived(goal.goal_id))
        elif status == "failed":
            self._finish(goal)
            self._emit(NavFailed(goal.goal_id, reason or "failed"))
        elif status == "cancelled":
            self._finish(goal)
            self._emit(NavCancelled(goal.goal_id, reason))

    def _finish(self, goal: Goal) -> None:
        with self.lock:
            if self._goal is goal:
                self._goal = None

    # ---- commands (queued, never block the caller) ----
    def enqueue(self, command: Command) -> None:
        self._commands.put(command)

    def send_goal(self, goal_id: str, x: float, y: float, yaw: float) -> None:
        """Drive to a map pose. A goal still being followed is replaced (the base does the same)."""
        with self.lock:
            self._goal = Goal(goal_id, x, y, yaw, sending=True, last_try=self.clock())
        self.enqueue(Command("goal", {"goal_id": goal_id, "x": round(x, 3), "y": round(y, 3), "yaw": round(yaw, 3)}))

    def reject(self, goal_id: str, reason: str) -> None:
        """A goal that was not sent (our own safety checks): report it as failed, on the bus, like the base's refusals."""
        self._emit(NavFailed(goal_id, reason))

    def cancel(self, goal_id: Optional[str] = None) -> None:
        """Stop the goal (the current one by default). The outcome, NavCancelled, is published when the base confirms."""
        with self.lock:
            goal = self._goal
            if goal is not None and goal_id in (None, goal.goal_id):
                goal.cancelling, goal_id = True, goal.goal_id
        if goal_id:
            self.enqueue(Command("cancel", {"goal_id": goal_id}))

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)
        self.enqueue(Command("speaking", {"speaking": self._speaking}))

    def set_max_speed(self, m_s: float) -> None:
        self._max_speed = float(m_s)
        if self._max_speed_supported:
            self.enqueue(Command("max_speed", {"m_s": round(self._max_speed, 3)}))

    def run_command(self, command: Command) -> bool:
        """Carry out one queued command, trying again (the commands are idempotent) when the link fails. True if it got through."""
        path = {"goal": "/api/goal", "cancel": "/api/cancel", "speaking": "/api/speaking", "max_speed": "/api/max_speed"}[command.kind]
        if command.kind == "max_speed" and not self._max_speed_supported:
            return True
        if command.kind == "cancel" and command.body.get("goal_id") == "*" and self._goal is not None:
            return True                      # the clear-up after a lost link: a goal sent since then is not a leftover
        for attempt in range(self.settings.retries + 1):
            if command.kind == "goal" and not self._wanted(command.body["goal_id"]):
                return True                                              # replaced or cancelled while it waited
            try:
                reply = self.http.post(path, command.body)
            except ApiError as exc:
                self._ok()                                               # it answered: the link works
                return self._refused(command, exc)
            except LinkError as exc:
                self._failed(exc)
                if attempt < self.settings.retries:
                    self.sleep(0.2 * (attempt + 1))
                continue
            self._ok()
            if command.kind == "goal":
                if reply.get("accepted") is True:
                    self._accepted(command.body["goal_id"])
                    return True
                reason = str(reply.get("reason") or "refused")
                if reason in TRANSIENT_REASONS and attempt < self.settings.retries:
                    self.sleep(0.6)
                    continue
                self._rejected(command.body["goal_id"], reason)
            return True
        if command.kind == "goal":
            goal = self._goal
            if goal is not None and goal.goal_id == command.body["goal_id"]:
                goal.sending = False                                     # the poller sends it again while the link lives
        return False

    def _wanted(self, goal_id: str) -> bool:
        goal = self._goal
        return goal is not None and goal.goal_id == goal_id

    def _accepted(self, goal_id: str) -> None:
        with self.lock:
            goal = self._goal
            if goal is not None and goal.goal_id == goal_id:
                goal.accepted_at, goal.sending = self.clock(), False
        log.info("[link] goal %s accepted", goal_id)

    def _rejected(self, goal_id: str, reason: str) -> None:
        with self.lock:
            goal = self._goal
            if goal is None or goal.goal_id != goal_id:
                return
            self._goal = None
        log.warning("[link] goal %s refused: %s", goal_id, reason)
        self._emit(NavFailed(goal_id, reason))

    def _refused(self, command: Command, exc: ApiError) -> bool:
        if command.kind == "max_speed" and exc.status == 404:
            self._max_speed_supported = False
            log.info("[link] the base has no /api/max_speed: not asking again")
            return True
        if command.kind == "goal":
            self._rejected(command.body["goal_id"], "ros_unavailable" if exc.status == 503 else f"rejected: {exc.error or exc.status}")
            return False
        log.warning("[link] %s: %s", command.kind, exc)
        return False

    def resend_unconfirmed(self) -> bool:
        """Queue the goal again if the base has not confirmed it and nothing is on its way. Called by the poller."""
        with self.lock:
            goal = self._goal
            if goal is None or goal.accepted_at is not None or goal.sending or goal.cancelling \
                    or self.clock() - goal.last_try < RESEND_EVERY_S or self.status == "lost":
                return False
            goal.sending, goal.last_try = True, self.clock()
            body = {"goal_id": goal.goal_id, "x": round(goal.x, 3), "y": round(goal.y, 3), "yaw": round(goal.yaw, 3)}
        self.enqueue(Command("goal", body))
        return True

    # ---- the threads ----
    def _loop(self, step: Callable[[], None], period_s: float) -> None:
        next_at = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            if now >= next_at:
                try:
                    step()
                except Exception:
                    log.exception("[link] %s failed", getattr(step, "__name__", "step"))
                next_at = max(next_at + period_s, self.clock())
            self._stop.wait(min(0.05, max(0.0, next_at - self.clock())))

    def _poll_step(self) -> None:
        self.poll_once()
        self.resend_unconfirmed()

    def _health_step(self) -> None:
        self.health_once()
        self.evaluate()

    def _sender(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=0.2)
            except queue.Empty:
                self.evaluate()
                continue
            if command is None:
                return
            try:
                self.run_command(command)
            except Exception:
                log.exception("[link] %s failed", command.kind)

    def start_threads(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self.health_once()                                               # first: is the base there, and our version?
        for name, target in (("heartbeat", lambda: self._loop(self.heartbeat_once, 1.0 / self.settings.heartbeat_hz)),
                             ("poll", lambda: self._poll_pair()), ("send", self._sender)):
            thread = threading.Thread(target=target, name=f"link-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _poll_pair(self) -> None:
        """State polls and (less often) health checks, on one thread."""
        health_period, last_health = self.settings.health_every_s, self.clock()
        state_period = 1.0 / self.settings.poll_hz
        next_at = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            try:
                if now >= next_at:
                    self._poll_step()
                    next_at = max(next_at + state_period, self.clock())
                if now - last_health >= health_period:
                    last_health = now
                    self._health_step()
                self.evaluate()
            except Exception:
                log.exception("[link] poll failed")
            self._stop.wait(min(0.05, max(0.0, next_at - self.clock())))

    def stop_threads(self) -> None:
        self._stop.set()
        self._commands.put(None)
        for thread in self._threads:
            thread.join(timeout=self.settings.timeout_s * 2 + 1)
        self._threads = []

    # ---- on the robot's event loop ----
    async def start(self, bus) -> None:
        """Deliver the link's events to the bus and start the threads. Call from the running loop; returns at once."""
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        self.sink = lambda event: loop.call_soon_threadsafe(events.put_nowait, event)

        async def pump() -> None:                                        # one by one, in order
            while True:
                await bus.publish(await events.get())

        self._pump = loop.create_task(pump())
        await loop.run_in_executor(None, self.start_threads)             # the first health check may take its timeout

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        await asyncio.get_running_loop().run_in_executor(None, self.stop_threads)

    # ---- for the operator page ----
    def fetch_map(self) -> Optional[dict]:
        """The Dell's map for the operator page (GET /api/map; blocking, run it in a thread). Returns None when there is
        no map, the base does not offer one (404: an older Dell), or it cannot be reached; else
        {"changed": bool, "version", "resolution", "width", "height", "origin": {"x", "y"}, "frame"} plus "png" (bytes) when
        it changed since the last call. It takes longer than a state poll (a picture), so it has its own, longer timeout, and it
        does not count towards the link's status."""
        path = "/api/map" if self._map_version is None else f"/api/map?have={self._map_version}"
        try:
            reply = self.http.get(path, MAP_TIMEOUT_S)
        except ApiError as exc:
            if exc.status == 404:
                log.info("[link] the base has no /api/map (an older robot_link_api): no map page")
            return None
        except LinkError:
            return None
        if not reply.get("ok") or not reply.get("available"):
            return None
        try:
            out = {k: reply[k] for k in ("version", "resolution", "width", "height", "origin", "frame")}
            out["changed"] = bool(reply.get("changed", True))
            if out["changed"]:
                out["png"] = base64.b64decode(reply["png_base64"], validate=True)
        except (KeyError, ValueError, TypeError):
            log.warning("[link] /api/map answered something that is not a map")
            return None
        self._map_version = out["version"]
        return out

    def info(self) -> dict:
        """What the operator page shows about the link (plain data)."""
        with self.lock:
            pose, nav, noise = dict(self.pose), dict(self.nav), dict(self.noise) if self.noise else None
            if self.status == "lost":                      # what the base last said is old news
                nav, noise = {"status": "unknown (link lost)"}, None
        return {"backend": "turtlebot", "url": self.settings.base_url, "status": self.status, "ros_ok": self.ros_ok,
                "localised": self.localised, "battery": self.battery, "clock_skew_s": self.clock_skew_s,
                "incompatible": self.incompatible, "pose": pose, "nav": nav, "noise": noise, "goal_id": self.goal_id}
