"""Robot face display: serves ui/ and pushes emotions to the page over Server-Sent Events.

Examples:
    python main.py display                    # http://localhost:8765, type emotions in this terminal
    python main.py display --port 9000

Terminal commands: an emotion name with optional hold seconds ("happy", "sad 5"), idle,
talk [seconds] (move the mouth, default 3), list, quit.
Over HTTP:
    curl -X POST localhost:8765/emotion -H 'Content-Type: application/json' -d '{"emotion":"happy","hold_s":3}'
    curl 'localhost:8765/emotion?name=happy&hold=3'
    curl 'localhost:8765/talk?seconds=3'                # seconds=0 stops
    curl -X POST localhost:8765/talk -d '{"levels":[0,0.5,1,0.4,0],"fps":30}'   # loudness envelope
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from modules.expression import DEFAULT_HOLD_S, DEFAULT_PORT, IDLE, make_event, make_talk_event, valid_names

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
KEEPALIVE_S = 15.0
MAX_BODY = 512 * 1024        # a 2-minute talk envelope is about 25 KB
DEFAULT_TALK_S = 3.0
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Hub:
    """Fans each event out to every connected page."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def publish(self, event: dict) -> int:
        with self._lock:
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

        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            first = lambda *keys: next((q[k][0] for k in keys if k in q), None)
            if url.path == "/events":
                self._events()
            elif url.path == "/emotion":
                name, hold = first("name", "emotion"), first("hold", "hold_s")
                self._emit(lambda: make_event(name or "", DEFAULT_HOLD_S if hold is None else hold))
            elif url.path == "/talk":
                seconds = first("seconds", "s")
                self._emit(lambda: make_talk_event(seconds=DEFAULT_TALK_S if seconds is None else seconds))
            else:
                self._static(url.path)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/emotion", "/talk"):
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
            if path == "/talk":
                self._emit(lambda: make_talk_event(data.get("levels"), data.get("fps", 30),
                                                   data.get("seconds"), data.get("delay_s", 0)))
                return
            name = data.get("emotion", data.get("name", ""))
            hold = data.get("hold_s", data.get("hold"))
            self._emit(lambda: make_event(name if isinstance(name, str) else "",
                                          DEFAULT_HOLD_S if hold is None else hold))

        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            target = (UI_DIR / rel).resolve()
            if UI_DIR not in target.parents or not target.is_file():
                self._json(404, {"error": "not found"})
                return
            self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

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


def make_server(hub: Hub, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> ThreadingHTTPServer:
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
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default all interfaces)")
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
