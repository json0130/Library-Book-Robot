"""The HTTP side of the API (docs/robot_link.md section 1): token, JSON, the error body, and the routes onto a LinkCore.

Standard library only. One thread per request (ThreadingHTTPServer); every handler answers from the core without waiting for ROS.
"""

import hmac
import json
import logging
import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from robot_link.core import LinkCore, error

log = logging.getLogger("robot_link.http")

MAX_BODY = 16 * 1024
DRAIN_LIMIT = 1024 * 1024               # an over-long body is read and dropped (up to this) so the 413 reaches the client
SOCKET_TIMEOUT_S = 5.0

GETS = {"/api/health", "/api/state"}
POSTS = {"/api/goal", "/api/cancel", "/api/speaking", "/api/heartbeat", "/api/max_speed"}


def number(body: dict, key: str) -> Optional[float]:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, core: LinkCore, token: str):
        self.core, self.token = core, token.encode("utf-8")
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):             # a client that hung up is not worth a traceback
        log.debug("connection from %s ended badly", client_address, exc_info=True)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.2}, name="robot_link_http", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S
    server_version = "robot_link/1"

    def log_message(self, fmt, *args):                            # never logs headers, so never the token
        log.debug("%s " + fmt, self.address_string(), *args)

    # ---- replies ----
    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def fail(self, status: int, code: str, detail: str = "") -> None:
        self.reply(*error(status, code, detail))

    # ---- routing ----
    def handle_any(self, method: str) -> None:
        try:
            self._handle(method)
        except (socket.timeout, ConnectionError):
            self.close_connection = True
        except Exception:
            log.exception("%s %s", method, self.path)
            try:
                self.fail(500, "internal", "a bug on the Dell; see its log")
            except OSError:
                self.close_connection = True

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        sent = (self.headers.get("X-Robot-Token") or "").encode("utf-8")
        if not hmac.compare_digest(sent, self.server.token):
            return self.fail(401, "unauthorised", "missing or wrong X-Robot-Token")
        if path not in GETS and path not in POSTS:
            return self.fail(404, "not_found", path)
        if method != ("GET" if path in GETS else "POST"):
            return self.fail(405, "method_not_allowed", "%s %s" % (method, path))
        core = self.server.core
        core.tick()
        if method == "GET":
            return self.reply(200, core.health() if path == "/api/health" else core.state())
        body = self.read_json()
        if body is None:
            return
        self.post(path, body, core)

    def read_json(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.fail(400, "bad_request", "bad Content-Length")
            return None
        if length < 0:
            self.fail(400, "bad_request", "bad Content-Length")
            return None
        if length > MAX_BODY:
            self.rfile.read(min(length, DRAIN_LIMIT))
            self.fail(413, "payload_too_large", "%d bytes (at most %d)" % (length, MAX_BODY))
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            self.fail(400, "bad_request", "the body is not JSON")
            return None
        if not isinstance(body, dict):
            self.fail(400, "bad_request", "the body must be a JSON object")
            return None
        return body

    def post(self, path: str, body: dict, core: LinkCore) -> None:
        if path == "/api/goal":
            gid = body.get("goal_id")
            x, y, yaw = number(body, "x"), number(body, "y"), number(body, "yaw")
            if not isinstance(gid, str) or not gid or len(gid) > 128 or None in (x, y, yaw):
                return self.fail(400, "bad_request", "goal_id (text) and x, y, yaw (finite numbers) are needed")
            return self.reply(*core.handle_goal(gid, x, y, yaw))
        if path == "/api/cancel":
            gid = body.get("goal_id")
            if not isinstance(gid, str) or not gid:
                return self.fail(400, "bad_request", "goal_id (text) is needed")
            return self.reply(*core.handle_cancel(gid))
        if path == "/api/speaking":
            if not isinstance(body.get("speaking"), bool):
                return self.fail(400, "bad_request", "speaking (true or false) is needed")
            return self.reply(*core.handle_speaking(body["speaking"]))
        if path == "/api/heartbeat":
            if number(body, "t") is None:
                return self.fail(400, "bad_request", "t (a number) is needed")
            return self.reply(*core.handle_heartbeat())
        if path == "/api/max_speed":
            m_s = number(body, "m_s")
            if m_s is None or m_s <= 0:
                return self.fail(400, "bad_request", "m_s (a number above 0) is needed")
            return self.reply(*core.handle_max_speed(m_s))

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_HEAD(self):
        self.handle_any("HEAD")
