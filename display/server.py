"""Robot face display: serves ui/ and pushes emotions to the page over Server-Sent Events.

Examples:
    python -m display.server                    # http://localhost:8765, type emotions in this terminal
    python -m display.server --port 9000

Terminal commands: an emotion name with optional hold seconds ("happy", "sad 5"), idle,
talk [seconds] (move the mouth, default 3), list, quit.
Over HTTP:
    curl -X POST localhost:8765/emotion -H 'Content-Type: application/json' -d '{"emotion":"happy","hold_s":3}'
    curl 'localhost:8765/emotion?name=happy&hold=3'
    curl 'localhost:8765/talk?seconds=3'                # seconds=0 stops
    curl -X POST localhost:8765/talk -d '{"levels":[0,0.5,1,0.4,0],"fps":30}'   # loudness envelope

The state machine's screen and the operator controls (see docs/state_machine.md):
    POST /screen   the robot's ScreenUpdate {"state_path","zone","layout","title","text","options"}: sent to every page
    POST /state    {"state","zone",...} for the operator page's live state line
    POST /input    a tap on a screen button {"choice": "return"}: goes into the inbox the robot polls
    POST /operator an operator command {"command": "goto", "arg": "OFFER"}: same inbox
    GET  /inbox?after=N   the inbox items after id N (what the robot polls)
    GET  /operator the operator page (events, zone toggle, goto, say). It listens on 127.0.0.1 unless
                   display.bind_host in config/robot.yaml (or --host) says otherwise: with 0.0.0.0 a laptop on the
                   same network can open http://<robot's address>:8765/operator. Such a laptop can only use
                   /operator (with a read-only face/screen preview in it); everything else is 403 unless the
                   request comes from the robot's own computer.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from robot.actuation.face import DEFAULT_HOLD_S, DEFAULT_PORT, IDLE, make_event, make_talk_event, valid_names

UI_DIR = Path(__file__).resolve().parent / "ui"
KEEPALIVE_S = 15.0
MAX_BODY = 512 * 1024        # a 2-minute talk envelope is about 25 KB
DEFAULT_TALK_S = 3.0
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


INPUT_CHOICES = ("return", "ask", "recommend", "more", "guide", "done", "another", "surprise")
CONTENT_CHOICE = re.compile(r"^(?:(?:book|where|guide|similar):\d{10,13}|(?:section|topic):[A-Za-z0-9 &',.-]{1,60}|floor:\d{1,2})$")
OPERATOR_COMMANDS = ("state", "goto", "zone", "engage", "busy", "leave", "dropped", "pick", "book", "say", "reset",
                     "screen", "next", "abort")
INBOX_KEEP = 200
REPLAYED = ("screen", "state")          # what a page that connects late is shown straight away


REMOTE_GET = ("/operator", "/operator.html", "/events", "/", "/index.html", "/camera.jpg", "/camera.mjpg", "/covers/")     # what another computer may read
REMOTE_POST = ("/operator",)                                                    # and the only thing it may send


def is_local(address: str) -> bool:
    """True for a request from the robot's own computer (127.0.0.1, ::1, ::ffff:127.0.0.1)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_loopback


def allowed(method: str, path: str, local: bool) -> bool:
    """The robot's own computer may do anything. Another computer may open the operator page (and the read-only
    preview of the face and screen inside it) and send operator commands, nothing else: not the robot's own
    posts (/screen, /state), the inbox the robot collects from, button taps (/input), or driving the face
    (/emotion, /talk)."""
    if local:
        return True
    if method == "GET" and path.startswith("/covers/"):
        return True
    return path in (REMOTE_GET if method == "GET" else REMOTE_POST)


class Inbox:
    """What people did on the pages (button taps, operator commands), for the robot to collect with /inbox?after=N."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._last = 0

    def add(self, item: dict) -> dict:
        with self._lock:
            self._last += 1
            item = {**item, "id": self._last}
            self._items.append(item)
            del self._items[:-INBOX_KEEP]
            return item

    def after(self, cursor: int) -> dict:
        with self._lock:
            return {"last": self._last, "items": [i for i in self._items if i["id"] > cursor]}


class Hub:
    """Fans each event out to every connected page, and remembers the last screen and state for pages that connect late."""

    def __init__(self, covers_dir=None):
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()
        self._latest: dict[str, dict] = {}
        self.covers_dir = covers_dir                # data/covers (books.covers_dir); None: looked up in the config
        self.inbox = Inbox()
        self.frame_cond = threading.Condition()
        self.frame: bytes | None = None             # the robot's newest camera picture (JPEG)
        self.frame_n = 0
        self.viewers = 0                            # pages watching the camera stream right now

    def set_frame(self, jpg: bytes) -> None:
        with self.frame_cond:
            self.frame, self.frame_n = jpg, self.frame_n + 1
            self.frame_cond.notify_all()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._clients.add(q)
            for event in self._latest.values():
                q.put(event)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def publish(self, event: dict) -> int:
        with self._lock:
            for key in REPLAYED:
                if key in event:
                    self._latest[key] = event
            clients = list(self._clients)
        for q in clients:
            q.put(event)
        return len(clients)


def parse_command(line: str) -> dict | None:
    """Terminal input -> event, e.g. "sad 5". Returns None for an empty line, raises ValueError if invalid."""
    parts = line.split()
    if not parts:
        return None
    talking = parts[0].lower() == "talk"
    seconds = DEFAULT_TALK_S if talking else DEFAULT_HOLD_S
    if len(parts) > 2:
        raise ValueError("expected: <emotion> [seconds] or talk [seconds]")
    if len(parts) == 2:
        try:
            seconds = float(parts[1])
        except ValueError:
            raise ValueError(f"time must be a number of seconds, got {parts[1]!r}") from None
    return make_talk_event(seconds=seconds) if talking else make_event(parts[0], seconds)


def ui_version() -> str:
    """Changes whenever a file of the page changes: an open page (a kiosk is never reloaded by hand) reloads itself."""
    return "-".join(f"{p.stat().st_mtime_ns:x}" for p in sorted(UI_DIR.glob("*")) if p.is_file() and p.suffix in (".html", ".js", ".css"))


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RobotFace"

        def log_message(self, fmt, *args):  # keep the terminal free for typing
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _emit(self, build) -> None:
            """Build an event (ValueError -> 400), broadcast it and reply with it."""
            try:
                event = build()
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            clients = hub.publish(event)
            reply = {**event, "clients": clients}
            if "talk" in event and "levels" in event["talk"]:   # don't echo the whole envelope
                talk = event["talk"]
                reply["talk"] = {"frames": len(talk["levels"]), "fps": talk["fps"], "delay_s": talk["delay_s"]}
            self._json(200, reply)

        def _denied(self, method: str, path: str) -> bool:
            if allowed(method, path, is_local(self.client_address[0])):
                return False
            self._json(403, {"error": "only the robot's own computer may use this; open /operator"})
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            if self._denied("GET", url.path):
                return
            q = parse_qs(url.query)
            first = lambda *keys: next((q[k][0] for k in keys if k in q), None)
            if url.path == "/events":
                self._events()
            elif url.path == "/camera.jpg":
                if hub.frame is None:
                    self._json(404, {"error": "no camera picture yet: run the simulator with --camera"})
                else:
                    self._send(200, hub.frame, "image/jpeg")
            elif url.path == "/camera.mjpg":
                self._mjpeg()
            elif url.path == "/ui-version":
                self._json(200, {"version": ui_version()})
            elif url.path == "/emotion":
                name, hold = first("name", "emotion"), first("hold", "hold_s")
                self._emit(lambda: make_event(name or "", DEFAULT_HOLD_S if hold is None else hold))
            elif url.path == "/talk":
                seconds = first("seconds", "s")
                self._emit(lambda: make_talk_event(seconds=DEFAULT_TALK_S if seconds is None else seconds))
            elif url.path == "/inbox":
                try:
                    cursor = int(first("after") or 0)
                except ValueError:
                    cursor = 0
                self._json(200, hub.inbox.after(cursor))
            elif url.path == "/operator":
                self._static("/operator.html")
            elif url.path.startswith("/covers/"):
                self._cover(url.path[len("/covers/"):], first("title") or "", first("author") or "")
            else:
                self._static(url.path)

        def do_POST(self):
            path = urlsplit(self.path).path
            if self._denied("POST", path):
                return
            if path == "/camera":
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_BODY:
                    self._json(400, {"error": "a JPEG picture of up to 512 KB"})
                    return
                body = self.rfile.read(length)
                if not body.startswith(b"\xff\xd8"):
                    self._json(400, {"error": "not a JPEG"})
                    return
                hub.set_frame(body)
                self._json(200, {"bytes": length, "viewers": hub.viewers})
                return
            if path not in ("/emotion", "/talk", "/screen", "/state", "/input", "/operator"):
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                self._json(400, {"error": 'body must be a JSON object, e.g. {"emotion": "happy", "hold_s": 3}'})
                return
            if path in ("/screen", "/state", "/input", "/operator"):
                self._state_post(path, data)
                return
            if path == "/talk":
                self._emit(lambda: make_talk_event(data.get("levels"), data.get("fps", 30),
                                                   data.get("seconds"), data.get("delay_s", 0)))
                return
            name = data.get("emotion", data.get("name", ""))
            hold = data.get("hold_s", data.get("hold"))
            self._emit(lambda: make_event(name if isinstance(name, str) else "",
                                          DEFAULT_HOLD_S if hold is None else hold))

        def _state_post(self, path: str, data: dict) -> None:
            """/screen and /state are the robot talking to the pages; /input and /operator are people talking to the robot."""
            if path == "/screen":
                if not isinstance(data.get("state_path"), str):
                    self._json(400, {"error": "a screen update needs a state_path"})
                    return
                self._json(200, {"clients": hub.publish({"screen": data})})
            elif path == "/state":
                self._json(200, {"clients": hub.publish({"state": data})})
            elif path == "/input":
                choice = data.get("choice")
                if choice not in INPUT_CHOICES and not (isinstance(choice, str) and CONTENT_CHOICE.match(choice)):
                    self._json(400, {"error": f"choice must be one of {', '.join(INPUT_CHOICES)}"})
                    return
                self._json(200, {"queued": hub.inbox.add({"type": "choice", "choice": choice})["id"]})
            else:
                command, arg = data.get("command"), data.get("arg", "")
                if command not in OPERATOR_COMMANDS or not isinstance(arg, str) or len(arg) > 500:
                    self._json(400, {"error": f"command must be one of {', '.join(OPERATOR_COMMANDS)}"})
                    return
                self._json(200, {"queued": hub.inbox.add({"type": "operator", "command": command, "arg": arg})["id"]})

        def _cover(self, isbn: str, title: str, author: str) -> None:
            """data/covers/<isbn>.jpg if it was downloaded, else a placeholder made from the title."""
            from robot.services.covers import cover_file, placeholder_svg
            isbn = isbn.removesuffix(".jpg")
            if not isbn.isdigit() or len(isbn) > 13:
                self._json(404, {"error": "not found"})
                return
            covers = hub.covers_dir
            if covers is None:
                from robot.config import load_config
                cfg = load_config()
                covers = hub.covers_dir = cfg.path(cfg.books.get("covers_dir") or "data/covers")
            path = cover_file(covers, isbn)
            if path is not None:
                self._send(200, path.read_bytes(), "image/jpeg")
            else:
                self._send(200, placeholder_svg(isbn, title[:120], author[:80]).encode(), "image/svg+xml")

        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            target = (UI_DIR / rel).resolve()
            if UI_DIR not in target.parents or not target.is_file():
                self._json(404, {"error": "not found"})
                return
            self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

        def _mjpeg(self) -> None:
            """The camera picture as a multipart stream the operator page shows in an <img>."""
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            seen = -1
            hub.viewers += 1
            try:
                while True:
                    with hub.frame_cond:
                        hub.frame_cond.wait_for(lambda: hub.frame_n != seen, timeout=KEEPALIVE_S)
                        jpg, seen = hub.frame, hub.frame_n
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.viewers -= 1

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=KEEPALIVE_S)
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def make_server(hub: Hub, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(hub))
    server.daemon_threads = True
    return server


def terminal_loop(hub: Hub) -> None:
    print("Type an emotion (optionally with seconds, e.g. 'sad 5'), idle, talk [seconds], list, or quit.")
    print("Emotions:", ", ".join(valid_names()))
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in ("quit", "exit", "q"):
            return
        if cmd in ("list", "help", "?"):
            print("Emotions:", ", ".join(valid_names()))
            continue
        try:
            event = parse_command(line)
        except ValueError as e:
            print(f"{e}\nValid: {', '.join(valid_names())}, talk [seconds], list, quit")
            continue
        if event is None:
            continue
        if "talk" in event:
            secs = event["talk"]["seconds"]
            print(f"talking for {secs:g}s" if secs else "stop talking")
        elif event["emotion"] == IDLE:
            print("idle")
        else:
            print(f"{event['emotion']} for {event['hold_s']:g}s")
        n = hub.publish(event)
        if n == 0:
            print("(no page connected yet)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
    from robot.config import load_config
    shown = load_config().display
    p.add_argument("--host", default=shown["bind_host"],
                   help=f"address to listen on (default {shown['bind_host']}, display.bind_host in config/robot.yaml; "
                        "0.0.0.0 lets another computer open /operator)")
    args = p.parse_args(argv)

    hub = Hub()
    try:
        server = make_server(hub, args.port, args.host)
    except OSError as e:
        print(f"error: cannot listen on {args.host}:{args.port} ({e})", file=sys.stderr)
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Robot face on http://localhost:{args.port}")
    try:
        terminal_loop(hub)
        if not sys.stdin.isatty():      # started without a terminal (e.g. by a script): keep serving
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
