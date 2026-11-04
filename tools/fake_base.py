"""A stand-in for the Dell on the TurtleBot: the full API v1 of docs/robot_link.md, standard library only.

    python tools/fake_base.py                              # http://127.0.0.1:8010, token from ROBOT_LINK_TOKEN
    python tools/fake_base.py --speed 2 --fail-rate 0.2    # drives faster; one goal in five fails on the way
    python tools/fake_base.py --drop-link-after 20         # 20 s after start the cable is "pulled" (see --drop-for)

What it does, so the robot's link code can be exercised without ROS:

    driving       a goal is driven in a straight line at --speed m/s (limited by /api/max_speed) with distance_left, and
                  ends succeeded; --fail-rate p makes a goal fail at a random point of the way (no_path / blocked)
    watchdog      no heartbeat for 3 s while driving: the base stops and cancels the goal (heartbeat_timeout)
    localisation  --unlocalised-for S: goals are refused (not_localised) for the first S seconds
    noise         a level per floor-plan area (quiet sections ~38 dB, everywhere else ~62 dB) with a little jitter, new
                  readings every 0.5 s; keyboard: + / - make it louder / quieter by 5 dB, 0 resets, d drops or restores the
                  link, f fails the next goal, q quits. While /api/speaking is true the level rises by 12 dB and the
                  readings say robot_noise: true (the robot's own voice)
    link drop     --drop-link-after N [--drop-for S]: from N seconds after start, connections are closed without an answer
                  (as with a pulled cable) for S seconds (default: for ever)
    --uncalibrated  reports raw dBFS (about -90 dB lower) with calibrated: false, like the real noise_logger today

`FakeBase` is the model (clock injected, so tests drive it by hand) and `FakeBaseServer` the HTTP server around it.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/fake_base.py`
sys.path.append(str(Path(__file__).resolve().parent.parent / "turtlebot"))   # the Dell's map picture code (standard library only)

log = logging.getLogger("fake_base")

API_VERSION = 1
HEARTBEAT_TIMEOUT_S = 3.0
SPEAKING_GRACE_S = 0.5
SPEAKING_CLEAR_S = 30.0
READING_EVERY_S = 0.5
MAX_BODY = 16 * 1024
HARDWARE_MAX_M_S = 2.0                     # the real base is capped at 0.25 m/s (hokuyo_costmap.yaml); the fake is quicker, so demos are
KNOWN_GOALS = 32
QUIET_DB, COMMON_DB, SPEAKING_EXTRA_DB, DBFS_OFFSET = 38.0, 62.0, 12.0, -90.0
FAIL_REASONS = ("no_path", "blocked", "aborted")


def read_sections(floorplan_path, shelves_path) -> tuple:
    """(plan or None, {section name lower: zone}) from the repo's data files; missing files give an empty answer."""
    plan, zones = None, {}
    try:
        from robot.services.floorplan import FloorPlan
        plan = FloorPlan.load(floorplan_path)
    except (OSError, ValueError, ImportError):
        pass
    try:
        votes: dict = {}
        with open(shelves_path, encoding="utf-8", newline="") as f:
            rows = csv.DictReader(line for line in f if not line.lstrip().startswith("#"))
            for row in rows:
                if row.get("section") and row.get("zone"):
                    counts = votes.setdefault(row["section"].lower(), {})
                    counts[row["zone"]] = counts.get(row["zone"], 0) + 1
        zones = {name: max(c, key=lambda z: (c[z], z == "quiet")) for name, c in votes.items()}
    except OSError:
        pass
    return plan, zones


class FakeBase:
    def __init__(self, token: str = "test-token", plan=None, section_zone: Optional[dict] = None,
                 start: Optional[tuple] = None, speed: float = 0.5, fail_rate: float = 0.0, seed: int = 1,
                 unlocalised_for: float = 0.0, uncalibrated: bool = False, level: Optional[int] = None, time_scale: float = 1.0,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.token, self.plan, self.section_zone = token, plan, dict(section_zone or {})
        self.clock, self.wall = clock, wall
        self.lock = threading.RLock()
        self.rng = random.Random(seed)
        self.speed, self.fail_rate, self.uncalibrated = float(speed), float(fail_rate), uncalibrated
        self.time_scale = float(time_scale)               # driving is this many times faster (scenario runs); the watchdog stays real time
        self.level = level if level is not None else (plan.station.level if plan is not None and plan.station else None)
        station = plan.station if plan is not None else None
        self.x, self.y, self.yaw = start or ((station.x, station.y, station.yaw) if station else (0.0, 0.0, 0.0))
        self.started = clock()
        self.unlocalised_until = self.started + float(unlocalised_for)
        self.ros_ok = True
        self.battery = 0.9
        self.max_speed = HARDWARE_MAX_M_S
        self.max_speed_offered = True
        self.last_heartbeat: Optional[float] = None
        self.speaking, self.speaking_since, self.speaking_until = False, 0.0, 0.0
        self.noise_offset_db = 0.0
        self.link_dropped = False
        self.fail_next = 0                                   # goals still to fail (the keyboard's f, or a test)
        self.goal: Optional[dict] = None                     # the goal being driven or last finished
        self.known: "OrderedDict[str, dict]" = OrderedDict() # the last KNOWN_GOALS goals by id
        self.last_update = clock()
        self._reading: Optional[tuple] = None                # (slot, dict) one reading per 0.5 s, the same for every poll
        self.history: list = []                              # what happened, for tests

    # ---- the model ----
    @property
    def localised(self) -> bool:
        return self.clock() >= self.unlocalised_until

    def note(self, what: str) -> None:
        self.history.append((round(self.clock() - self.started, 2), what))
        log.info("%s", what)

    def update(self) -> None:
        """Advance the simulation to now: drive, and apply the heartbeat watchdog."""
        with self.lock:
            now = self.clock()
            dt, self.last_update = max(0.0, now - self.last_update), now
            if self.speaking and now - self.speaking_since > SPEAKING_CLEAR_S:
                self.speaking = False                        # the Jetson went away while talking
            g = self.goal
            if g is None or g["status"] != "active":
                return
            silent = now - (self.last_heartbeat if self.last_heartbeat is not None else g["accepted"])
            if silent > HEARTBEAT_TIMEOUT_S:
                self.finish("cancelled", "heartbeat_timeout")
                self.note(f"no heartbeat for {silent:.1f} s: stopped, goal {g['goal_id']} cancelled")
                return
            if not self.localised:
                self.finish("failed", "lost_localisation")
                return
            step = min(self.speed, self.max_speed) * self.time_scale * dt
            dx, dy = g["x"] - self.x, g["y"] - self.y
            left = math.hypot(dx, dy)
            if g["fail_at"] is not None and g["total"] - left >= g["fail_at"]:
                self.finish("failed", g["fail_reason"])
                self.note(f"goal {g['goal_id']} failed on the way ({g['fail_reason']})")
                return
            if step >= left:
                self.x, self.y, self.yaw = g["x"], g["y"], g["yaw"]
                self.finish("succeeded", "")
                self.note(f"arrived at ({self.x:.2f}, {self.y:.2f}) for goal {g['goal_id']}")
            else:
                self.x, self.y = self.x + dx / left * step, self.y + dy / left * step
                self.yaw = math.atan2(dy, dx)

    def finish(self, status: str, reason: str) -> None:
        g = self.goal
        if g is not None:
            g["status"], g["reason"] = status, reason

    def distance_left(self) -> Optional[float]:
        g = self.goal
        if g is None or g["status"] != "active":
            return None
        return round(math.hypot(g["x"] - self.x, g["y"] - self.y), 2)

    # ---- the noise ----
    def zone_here(self) -> str:
        if self.plan is not None and self.level is not None:
            floor = self.plan.floor(self.level)
            for section in (floor.sections if floor else ()):
                if section.contains(self.x, self.y):
                    return getattr(section, "zone", None) or self.section_zone.get(section.name.lower(), "common")
        return "common"

    def reading(self) -> Optional[dict]:
        now = self.wall()
        slot = math.floor(now / READING_EVERY_S)
        with self.lock:
            if self._reading is not None and self._reading[0] == slot:
                return self._reading[1]
            robot = self.speaking or now < self.speaking_until
            level = (QUIET_DB if self.zone_here() == "quiet" else COMMON_DB) + self.noise_offset_db \
                + self.rng.uniform(-1.5, 1.5) + (SPEAKING_EXTRA_DB if self.speaking else 0.0)
            level += DBFS_OFFSET if self.uncalibrated else 0.0
            balance = round(self.rng.uniform(-2.0, 2.0), 1)
            body = {"overall": round(level, 1), "left": round(level + balance / 2, 1), "right": round(level - balance / 2, 1),
                    "balance": balance, "unit": "dBFS" if self.uncalibrated else "dB", "calibrated": not self.uncalibrated,
                    "robot_noise": bool(robot), "t": round(slot * READING_EVERY_S, 3)}
            self._reading = (slot, body)
            return body

    def make_map(self):
        """The floor plan's rectangle as an occupancy grid (a wall round the edge, a metre of unknown outside): what the Dell's /map would be."""
        from robot_link import rules
        floor = self.plan.floor(self.level) if self.plan is not None and self.level is not None else None
        width_m, height_m = (floor.width, floor.height) if floor is not None else (8.0, 6.0)
        x0, y0 = (floor.x0, floor.y0) if floor is not None else (0.0, 0.0)
        res, pad = 0.05, 20
        w, h = int(width_m / res) + 2 * pad, int(height_m / res) + 2 * pad
        data = [-1] * (w * h)
        for cy in range(pad, h - pad):
            for cx in range(pad, w - pad):
                edge = cx in (pad, w - pad - 1) or cy in (pad, h - pad - 1)
                data[cy * w + cx] = 100 if edge else 0
        return rules.GridView(w, h, res, x0 - pad * res, y0 - pad * res, tuple(data))

    def map_info(self, have: Optional[int] = None) -> dict:
        import base64
        from robot_link import mapimage
        if getattr(self, "_map_image", None) is None:
            self._map_image = mapimage.render(self.make_map())
        image = self._map_image
        body = {"ok": True, "available": True, "frame": "map", "version": image["version"], "resolution": image["resolution"],
                "width": image["width"], "height": image["height"], "origin": {"x": image["origin_x"], "y": image["origin_y"]},
                "time": round(self.wall(), 3), "changed": have != image["version"]}
        if body["changed"]:
            body["png_base64"] = base64.b64encode(image["png"]).decode("ascii")
        return body

    # ---- the API ----
    def health(self) -> dict:
        self.update()
        return {"ok": True, "api_version": API_VERSION, "ros_ok": self.ros_ok, "localised": self.localised,
                "battery": round(self.battery, 2), "time": round(self.wall(), 3)}

    def state(self) -> dict:
        self.update()
        g = self.goal
        return {"pose": {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw, 3), "frame": "map",
                         "localised": self.localised, "t": round(self.wall(), 3)},
                "nav": {"status": g["status"] if g else "idle", "goal_id": g["goal_id"] if g else "",
                        "distance_left": self.distance_left(), "reason": g["reason"] if g else ""},
                "noise": self.reading(), "time": round(self.wall(), 3)}

    def heartbeat(self) -> dict:
        self.update()
        self.last_heartbeat = self.clock()
        return {"ok": True, "time": round(self.wall(), 3)}

    def send_goal(self, goal_id: str, x: float, y: float, yaw: float) -> tuple:
        """(http status, body)."""
        self.update()
        with self.lock:
            if goal_id in self.known:
                return 200, {"ok": True, "accepted": True, "duplicate": True, "goal_id": goal_id, "reason": ""}
            refuse = None
            if not self.ros_ok:
                return 503, {"ok": False, "error": "ros_unavailable", "detail": "move_base is not running"}
            if self.last_heartbeat is None or self.clock() - self.last_heartbeat > HEARTBEAT_TIMEOUT_S:
                refuse = "no_heartbeat"
            elif not self.localised:
                refuse = "not_localised"
            elif not (-math.pi - 1e-3 <= yaw <= math.pi + 1e-3):
                refuse = "invalid_pose"
            elif self.outside_map(x, y):
                refuse = "outside_map"
            if refuse:
                self.note(f"goal {goal_id} refused: {refuse}")
                return 200, {"ok": True, "accepted": False, "goal_id": goal_id, "reason": refuse}
            if self.goal is not None and self.goal["status"] == "active":
                self.finish("cancelled", "superseded")
            total = math.hypot(x - self.x, y - self.y)
            fail = self.fail_next > 0 or self.rng.random() < self.fail_rate
            if self.fail_next > 0:
                self.fail_next -= 1
            self.goal = {"goal_id": goal_id, "x": x, "y": y, "yaw": yaw, "status": "active", "reason": "", "total": total,
                         "accepted": self.clock(), "fail_at": self.rng.uniform(0.2, 0.8) * total if fail else None,
                         "fail_reason": self.rng.choice(FAIL_REASONS)}
            self.known[goal_id] = self.goal
            while len(self.known) > KNOWN_GOALS:
                self.known.popitem(last=False)
            self.note(f"goal {goal_id} accepted: ({x:.2f}, {y:.2f}, {yaw:.2f}), {total:.1f} m"
                      + (" [will fail]" if fail else ""))
            return 200, {"ok": True, "accepted": True, "goal_id": goal_id, "reason": ""}

    def outside_map(self, x: float, y: float) -> bool:
        floor = self.plan.floor(self.level) if self.plan is not None and self.level is not None else None
        return floor is not None and not floor.contains(x, y)

    def cancel(self, goal_id: str) -> dict:
        self.update()
        with self.lock:
            g = self.goal
            hit = g is not None and g["status"] == "active" and goal_id in ("*", g["goal_id"])
            if hit:
                self.finish("cancelled", "cancelled_by_client")
                self.note(f"goal {g['goal_id']} cancelled by the robot")
            return {"ok": True, "cancelled": bool(hit)}

    def set_speaking(self, speaking: bool) -> dict:
        with self.lock:
            now = self.clock()
            if speaking:
                self.speaking, self.speaking_since = True, now
            elif self.speaking:
                self.speaking, self.speaking_until = False, self.wall() + SPEAKING_GRACE_S
        return {"ok": True}

    def set_max_speed(self, m_s: float) -> Optional[dict]:
        if not self.max_speed_offered:
            return None
        self.max_speed = min(float(m_s), HARDWARE_MAX_M_S)
        return {"ok": True, "applied": self.max_speed}


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeBase/1"
    protocol_version = "HTTP/1.0"

    base: FakeBase                                        # set on the subclass made by FakeBaseServer

    def log_message(self, fmt, *args):                    # quiet: the model logs what matters
        pass

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, code: str, detail: str = "") -> None:
        self.reply(status, {"ok": False, "error": code, "detail": detail})

    def handle_any(self, method: str) -> None:
        if self.server.dropped():                         # a pulled cable: no answer at all
            self.close_connection = True
            return
        path = self.path.split("?", 1)[0]
        if self.headers.get("X-Robot-Token") != self.base.token:
            return self.error(401, "unauthorised", "missing or wrong X-Robot-Token")
        have = None
        for part in self.path.partition("?")[2].split("&"):
            if part.startswith("have="):
                have = int(part[5:]) if part[5:].lstrip("-").isdigit() else None
        gets = {"/api/health": self.base.health, "/api/state": self.base.state, "/api/map": lambda: self.base.map_info(have)}
        posts = {"/api/goal", "/api/cancel", "/api/speaking", "/api/heartbeat", "/api/max_speed"}
        if path not in gets and path not in posts:
            return self.error(404, "not_found", path)
        if (method == "GET") != (path in gets):
            return self.error(405, "method_not_allowed", f"{method} {path}")
        if method == "GET":
            return self.reply(200, gets[path]())
        body = self.read_json()
        if body is None:
            return
        self.post(path, body)

    def read_json(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.error(400, "bad_request", "bad Content-Length")
            return None
        if length > MAX_BODY:
            self.error(413, "payload_too_large", f"{length} bytes")
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self.error(400, "bad_request", "the body is not JSON")
            return None
        if not isinstance(body, dict):
            self.error(400, "bad_request", "the body must be a JSON object")
            return None
        return body

    @staticmethod
    def numbers(body: dict, *keys) -> Optional[list]:
        out = []
        for key in keys:
            v = body.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return None
            out.append(float(v))
        return out

    def post(self, path: str, body: dict) -> None:
        base = self.base
        if path == "/api/goal":
            gid, nums = body.get("goal_id"), self.numbers(body, "x", "y", "yaw")
            if not isinstance(gid, str) or not gid or nums is None:
                return self.error(400, "bad_request", "goal_id (text) and x, y, yaw (numbers) are needed")
            status, reply = base.send_goal(gid, *nums)
            return self.reply(status, reply)
        if path == "/api/cancel":
            gid = body.get("goal_id")
            if not isinstance(gid, str) or not gid:
                return self.error(400, "bad_request", "goal_id (text) is needed")
            return self.reply(200, base.cancel(gid))
        if path == "/api/speaking":
            if not isinstance(body.get("speaking"), bool):
                return self.error(400, "bad_request", "speaking (true or false) is needed")
            return self.reply(200, base.set_speaking(body["speaking"]))
        if path == "/api/heartbeat":
            if self.numbers(body, "t") is None:
                return self.error(400, "bad_request", "t (a number) is needed")
            return self.reply(200, base.heartbeat())
        if path == "/api/max_speed":
            nums = self.numbers(body, "m_s")
            if nums is None or nums[0] <= 0:
                return self.error(400, "bad_request", "m_s (a number above 0) is needed")
            reply = base.set_max_speed(nums[0])
            return self.reply(200, reply) if reply is not None else self.error(404, "not_found", "/api/max_speed")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.error(405, "method_not_allowed", "PUT")

    do_DELETE = do_PATCH = do_PUT


class FakeBaseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, base: FakeBase, host: str = "127.0.0.1", port: int = 0, drop_after_s: Optional[float] = None,
                 drop_for_s: Optional[float] = None):
        self.model = base
        handler = type("BoundHandler", (Handler,), {"base": base})
        super().__init__((host, port), handler)
        self.drop_after_s, self.drop_for_s = drop_after_s, drop_for_s
        self.opened = time.monotonic()
        self._thread: Optional[threading.Thread] = None
        self._ticker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.server_address[0]}:{self.port}"

    def dropped(self) -> bool:
        if self.model.link_dropped:
            return True
        if self.drop_after_s is None:
            return False
        elapsed = time.monotonic() - self.opened
        return elapsed >= self.drop_after_s and (self.drop_for_s is None or elapsed < self.drop_after_s + self.drop_for_s)

    def start(self) -> "FakeBaseServer":
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-base", daemon=True)
        self._thread.start()
        self._ticker = threading.Thread(target=self._tick, name="fake-base-tick", daemon=True)
        self._ticker.start()
        return self

    def _tick(self) -> None:
        """The base drives and its watchdog runs even when nobody asks."""
        while not self._stop.wait(0.05):
            self.model.update()

    def stop(self) -> None:
        self._stop.set()
        self.shutdown()
        self.server_close()


def read_keys(server: FakeBaseServer, stop: threading.Event) -> None:
    """+ louder, - quieter, 0 reset, d drop / restore the link, f fail the next goal, q quit."""
    base = server.model
    interactive = sys.stdin.isatty()
    restore = None
    if interactive:
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            restore = (fd, termios.tcgetattr(fd))
            tty.setcbreak(fd)
        except (ImportError, OSError):
            interactive = False
    try:
        while not stop.is_set():
            key = sys.stdin.read(1) if interactive else sys.stdin.readline()[:1]
            if key == "" or key == "\n" and not interactive:
                if key == "":                                  # the input ended (a script, /dev/null): no keyboard, keep serving
                    break
                continue
            if key in ("+", "="):
                base.noise_offset_db += 5.0
            elif key == "-":
                base.noise_offset_db -= 5.0
            elif key == "0":
                base.noise_offset_db = 0.0
            elif key == "d":
                base.link_dropped = not base.link_dropped
                log.info("link %s", "DROPPED" if base.link_dropped else "restored")
            elif key == "f":
                base.fail_next += 1
                log.info("the next goal will fail")
            elif key == "q":
                stop.set()
                break
            else:
                continue
            log.info("noise offset %+.0f dB", base.noise_offset_db)
    finally:
        if restore:
            import termios
            termios.tcsetattr(restore[0], termios.TCSADRAIN, restore[1])


def main(argv=None) -> int:
    from robot.config import ROOT, load_config
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default %(default)s)")
    p.add_argument("--port", type=int, default=8010)
    p.add_argument("--token", default=os.environ.get("ROBOT_LINK_TOKEN", ""), help="default: $ROBOT_LINK_TOKEN")
    p.add_argument("--speed", type=float, default=0.5, help="driving speed in m/s (default %(default)s)")
    p.add_argument("--fail-rate", type=float, default=0.0, help="chance that a goal fails on the way (0..1)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--drop-link-after", type=float, default=None, metavar="SECONDS")
    p.add_argument("--drop-for", type=float, default=None, metavar="SECONDS", help="how long the link stays dropped (default: for ever)")
    p.add_argument("--unlocalised-for", type=float, default=0.0, metavar="SECONDS")
    p.add_argument("--uncalibrated", action="store_true", help="report raw dBFS with calibrated: false")
    p.add_argument("--start", default=None, metavar="X,Y,YAW", help="where the base starts (default: the floor plan's station)")
    p.add_argument("--floor", type=int, default=None, help="the floor of the plan the base is on, for its noise (default: the station's)")
    p.add_argument("--time-scale", type=float, default=1.0, help="drive this many times faster (demos and tests; the 3 s watchdog stays real time)")
    p.add_argument("--floorplan", type=Path, default=cfg.path(cfg.books["floorplan_path"]))
    p.add_argument("--shelves", type=Path, default=cfg.path(cfg.books["shelves_path"]))
    p.add_argument("--no-max-speed", action="store_true", help="do not offer /api/max_speed (404)")
    p.add_argument("--quiet", action="store_true", help="no log lines")
    args = p.parse_args(argv)
    if not args.token:
        print("fake_base: set ROBOT_LINK_TOKEN (or --token): the same secret the robot uses", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="[fake_base] %(message)s", stream=sys.stderr)
    plan, zones = read_sections(args.floorplan, args.shelves)
    start = tuple(float(v) for v in args.start.split(",")) if args.start else None
    base = FakeBase(args.token, plan, zones, start, args.speed, args.fail_rate, args.seed, args.unlocalised_for, args.uncalibrated,
                    level=args.floor, time_scale=args.time_scale)
    base.max_speed_offered = not args.no_max_speed
    server = FakeBaseServer(base, args.host, args.port, args.drop_link_after, args.drop_for).start()
    print(f"fake_base: {server.url}  (token from {'--token' if args.token != os.environ.get('ROBOT_LINK_TOKEN') else 'ROBOT_LINK_TOKEN'}); "
          f"keys: + - 0 d f q", file=sys.stderr, flush=True)
    stop = threading.Event()
    if sys.stdin and not sys.stdin.closed:
        threading.Thread(target=read_keys, args=(server, stop), daemon=True).start()
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
