import io
import json
import math
import threading
import wave
from array import array
import unittest
import urllib.error
import urllib.request

from display.server import Hub, make_server, parse_command
from robot.actuation.face import (DEFAULT_HOLD_S, make_event, make_talk_event, mouth_levels, normalize,
                                set_emotion, talk)


def wav_bytes(samples, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array("h", samples).tobytes())
    return buf.getvalue()


class ExpressionNamesTest(unittest.TestCase):
    def test_server_model_labels_and_aliases(self):
        for label in ("angry", "disgust", "fear", "happy", "sad", "surprise", "neutral"):
            self.assertEqual(normalize(label), label)
        self.assertEqual(normalize("Surprised"), "surprise")
        self.assertEqual(normalize(" SCARED "), "fear")
        self.assertEqual(normalize("mad"), "angry")
        self.assertEqual(normalize("joy"), "happy")
        self.assertEqual(normalize("idle"), "idle")

    def test_unknown_name_is_rejected(self):
        self.assertIsNone(normalize("confused"))
        with self.assertRaises(ValueError):
            make_event("confused")

    def test_event_format_and_default_hold(self):
        self.assertEqual(make_event("happy"), {"emotion": "happy", "hold_s": DEFAULT_HOLD_S})
        self.assertEqual(DEFAULT_HOLD_S, 3.0)
        self.assertEqual(make_event("sad", "5"), {"emotion": "sad", "hold_s": 5.0})

    def test_bad_hold_is_rejected(self):
        for bad in (0, -1, "abc", None, 1e9):
            with self.assertRaises(ValueError):
                make_event("happy", bad)


class TalkTest(unittest.TestCase):
    def test_talk_events(self):
        self.assertEqual(make_talk_event(seconds=2), {"talk": {"seconds": 2.0, "delay_s": 0.0}})
        self.assertEqual(make_talk_event(seconds=0)["talk"]["seconds"], 0.0)        # stop
        ev = make_talk_event([0, 0.5, 2, -1], fps=30, delay_s=0.1)["talk"]
        self.assertEqual(ev, {"levels": [0.0, 0.5, 1.0, 0.0], "fps": 30.0, "delay_s": 0.1})

    def test_invalid_talk_events(self):
        for kwargs in ({}, {"seconds": -1}, {"seconds": "x"}, {"seconds": 1e6}, {"levels": ["a"]},
                       {"levels": [0.5], "fps": 0}, {"levels": [0.5] * 100000}, {"seconds": 1, "delay_s": 9}):
            with self.assertRaises(ValueError, msg=kwargs):
                make_talk_event(**kwargs)

    def test_mouth_levels_follow_loudness(self):
        rate, fps = 24000, 30
        tone = [int(8000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate // 2)]
        quiet = [0] * (rate // 2)
        levels = mouth_levels(wav_bytes(quiet + tone + quiet, rate), fps)
        self.assertEqual(len(levels), 45)                 # 1.5 s at 30 fps
        self.assertTrue(all(v == 0 for v in levels[:14]))  # silence: mouth shut
        self.assertTrue(all(v > 0.9 for v in levels[16:29]))  # speech: mouth open
        self.assertTrue(all(v == 0 for v in levels[31:]))
        self.assertEqual(mouth_levels(wav_bytes([])), [])


class ParseCommandTest(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse_command("happy"), {"emotion": "happy", "hold_s": 3.0})
        self.assertEqual(parse_command("  SAD 5 "), {"emotion": "sad", "hold_s": 5.0})
        self.assertEqual(parse_command("idle")["emotion"], "idle")
        self.assertIsNone(parse_command("   "))

    def test_talk_command(self):
        self.assertEqual(parse_command("talk"), {"talk": {"seconds": 3.0, "delay_s": 0.0}})
        self.assertEqual(parse_command("TALK 0")["talk"]["seconds"], 0.0)

    def test_invalid_commands(self):
        for bad in ("confused", "happy abc", "happy 2 3", "happy -1", "talk x", "talk -2"):
            with self.assertRaises(ValueError):
                parse_command(bad)


class DisplayServerTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.server = make_server(self.hub, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, body):
        req = urllib.request.Request(self.url("/emotion"), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def listen(self):
        """Open /events and wait until the server has registered the client."""
        resp = urllib.request.urlopen(self.url("/events"), timeout=5)
        self.streams.append(resp)
        self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
        self.assertEqual(resp.readline(), b": connected\n")
        resp.readline()
        return resp

    @staticmethod
    def read_event(resp):
        line = resp.readline().decode()
        resp.readline()
        assert line.startswith("data: "), line
        return json.loads(line[len("data: "):])

    def wait_for_clients(self, n):
        for _ in range(100):
            if len(self.hub._clients) == n:
                return
            threading.Event().wait(0.02)
        self.fail("clients did not connect")

    def test_post_broadcasts_to_all_clients(self):
        clients = [self.listen() for _ in range(3)]
        self.wait_for_clients(3)
        with self.post({"emotion": "Surprised", "hold_s": 2}) as r:
            body = json.loads(r.read())
        self.assertEqual(body["clients"], 3)
        for c in clients:
            self.assertEqual(self.read_event(c), {"emotion": "surprise", "hold_s": 2.0})

    def test_post_default_hold(self):
        c = self.listen()
        self.wait_for_clients(1)
        self.post({"emotion": "happy"}).close()
        self.assertEqual(self.read_event(c), {"emotion": "happy", "hold_s": 3.0})

    def test_get_endpoint(self):
        c = self.listen()
        self.wait_for_clients(1)
        with urllib.request.urlopen(self.url("/emotion?name=happy&hold=4"), timeout=5) as r:
            self.assertEqual(json.loads(r.read())["emotion"], "happy")
        self.assertEqual(self.read_event(c), {"emotion": "happy", "hold_s": 4.0})
        urllib.request.urlopen(self.url("/emotion?name=sad"), timeout=5).close()
        self.assertEqual(self.read_event(c), {"emotion": "sad", "hold_s": 3.0})

    def test_invalid_requests_return_400_and_broadcast_nothing(self):
        c = self.listen()
        self.wait_for_clients(1)
        for call in (lambda: urllib.request.urlopen(self.url("/emotion?name=confused"), timeout=5),
                     lambda: urllib.request.urlopen(self.url("/emotion"), timeout=5),
                     lambda: self.post({"emotion": "happy", "hold_s": "abc"}),
                     lambda: self.post({"emotion": "happy", "hold_s": -2}),
                     lambda: self.post({"emotion": 5})):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                call()
            self.assertEqual(cm.exception.code, 400)
        req = urllib.request.Request(self.url("/emotion"), data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        self.post({"emotion": "idle"}).close()          # first event the client sees
        self.assertEqual(self.read_event(c)["emotion"], "idle")

    def test_serves_the_page_and_blocks_path_escape(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as r:
            self.assertIn(b"<canvas", r.read())
        for path in ("/../main.py", "/nope.html"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.url(path), timeout=5)
            self.assertEqual(cm.exception.code, 404)

    def test_set_emotion_helper(self):
        c = self.listen()
        self.wait_for_clients(1)
        self.assertTrue(set_emotion("scared", 1.5, port=self.port))
        self.assertEqual(self.read_event(c), {"emotion": "fear", "hold_s": 1.5})
        with self.assertRaises(ValueError):
            set_emotion("confused", port=self.port)

    def test_talk_endpoints(self):
        c = self.listen()
        self.wait_for_clients(1)
        with urllib.request.urlopen(self.url("/talk?seconds=2"), timeout=5) as r:
            self.assertEqual(json.loads(r.read())["clients"], 1)
        self.assertEqual(self.read_event(c), {"talk": {"seconds": 2.0, "delay_s": 0.0}})
        urllib.request.urlopen(self.url("/talk"), timeout=5).close()               # default 3 s
        self.assertEqual(self.read_event(c)["talk"]["seconds"], 3.0)
        levels = [0.5] * 3000                                                      # 100 s envelope
        self.assertTrue(talk(levels=levels, delay_s=0.1, port=self.port))
        self.assertEqual(self.read_event(c), {"talk": {"levels": levels, "fps": 30.0, "delay_s": 0.1}})
        req = urllib.request.Request(self.url("/talk"), data=b'{"seconds": -1}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_set_emotion_returns_false_when_display_is_down(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertFalse(set_emotion("happy", port=self.port, timeout=0.5))
        self.assertFalse(talk(seconds=1, port=self.port, timeout=0.5))
        self.server = make_server(Hub(), port=0, host="127.0.0.1")   # so tearDown has something to close
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class StateAndOperatorEndpointsTest(DisplayServerTest):
    """The state machine's screen, the touch-button input and the operator page (display server side)."""

    def post_to(self, path, body):
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def get_json(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return json.loads(r.read())

    SCREEN = {"state_path": "ENGAGED.OFFER", "zone": "quiet", "layout": "split", "title": "How can I help?",
              "text": "Hello", "options": [{"id": "return", "label": "Return a book"}]}

    def test_a_screen_update_reaches_every_page_on_the_event_stream(self):
        clients = [self.listen() for _ in range(2)]
        self.wait_for_clients(2)
        with self.post_to("/screen", self.SCREEN) as r:
            self.assertEqual(json.loads(r.read())["clients"], 2)
        for c in clients:
            self.assertEqual(self.read_event(c), {"screen": self.SCREEN})

    def test_a_page_that_connects_late_is_shown_the_last_screen_and_state(self):
        self.post_to("/screen", self.SCREEN).close()
        self.post_to("/state", {"state": "ENGAGED.OFFER", "zone": "quiet"}).close()
        self.post_to("/screen", {**self.SCREEN, "state_path": "ENGAGED.SCAN"}).close()
        c = self.listen()
        seen = {k: v for _ in range(2) for k, v in self.read_event(c).items()}
        self.assertEqual(seen["screen"]["state_path"], "ENGAGED.SCAN")                # the newest one only
        self.assertEqual(seen["state"]["zone"], "quiet")

    def test_a_screen_update_needs_a_state_path(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post_to("/screen", {"title": "x"})
        self.assertEqual(cm.exception.code, 400)

    def test_input_queues_a_choice_for_the_robot(self):
        self.assertEqual(self.get_json("/inbox?after=0"), {"last": 0, "items": []})
        with self.post_to("/input", {"choice": "return"}) as r:
            self.assertEqual(json.loads(r.read()), {"queued": 1})
        self.post_to("/input", {"choice": "done"}).close()
        inbox = self.get_json("/inbox?after=0")
        self.assertEqual(inbox["last"], 2)
        self.assertEqual([(i["id"], i["type"], i["choice"]) for i in inbox["items"]],
                         [(1, "choice", "return"), (2, "choice", "done")])
        self.assertEqual([i["id"] for i in self.get_json("/inbox?after=1")["items"]], [2])
        self.assertEqual(self.get_json("/inbox?after=2")["items"], [])

    def test_input_rejects_unknown_choices(self):
        for body in ({"choice": "launch"}, {"choice": 5}, {}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post_to("/input", body)
            self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.get_json("/inbox")["items"], [])

    def test_operator_commands_are_queued_and_checked(self):
        with self.post_to("/operator", {"command": "goto", "arg": "OFFER"}) as r:
            self.assertEqual(json.loads(r.read()), {"queued": 1})
        self.post_to("/operator", {"command": "engage"}).close()
        items = self.get_json("/inbox")["items"]
        self.assertEqual([(i["type"], i["command"], i["arg"]) for i in items], [("operator", "goto", "OFFER"), ("operator", "engage", "")])
        for body in ({"command": "rm -rf"}, {"command": "say", "arg": 5}, {"command": "say", "arg": "x" * 501}, {}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post_to("/operator", body)
            self.assertEqual(cm.exception.code, 400)

    def test_the_operator_page_and_the_screen_page_are_served(self):
        with urllib.request.urlopen(self.url("/operator"), timeout=5) as r:
            page = r.read().decode()
            self.assertIn("text/html", r.headers["Content-Type"])
        for needle in ("Robot operator", "/operator", "EventSource", "goto", "zone-quiet", "reset"):
            self.assertIn(needle, page)
        with urllib.request.urlopen(self.url("/"), timeout=5) as r:
            index = r.read().decode()
        for needle in ("options", "/input", "showScreen"):
            self.assertIn(needle, index)

    def test_the_server_listens_on_localhost_by_default(self):
        from robot.config import load_config
        from robot.config import DISPLAY_DEFAULTS
        self.assertEqual(DISPLAY_DEFAULTS["bind_host"], "127.0.0.1")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(make_server.__defaults__[1], "127.0.0.1")

    def test_another_computer_may_only_use_the_operator_page(self):
        from display.server import allowed, is_local
        for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(is_local(addr), addr)
        for addr in ("172.24.10.190", "192.168.1.5", "fe80::1%eth0", "nonsense"):
            self.assertFalse(is_local(addr), addr)
        for method, path in (("GET", "/operator"), ("GET", "/events"), ("GET", "/"), ("POST", "/operator")):
            self.assertTrue(allowed(method, path, False), (method, path))
        for method, path in (("POST", "/input"), ("POST", "/screen"), ("POST", "/state"), ("POST", "/emotion"),
                             ("POST", "/talk"), ("GET", "/inbox"), ("GET", "/emotion"), ("GET", "/talk")):
            self.assertFalse(allowed(method, path, False), (method, path))
            self.assertTrue(allowed(method, path, True), (method, path))

    def test_the_camera_picture_is_posted_by_the_robot_and_streamed_to_the_page(self):
        jpg = b"\xff\xd8\xff\xe0fake-jpeg-data\xff\xd9"
        with self.assertRaises(urllib.error.HTTPError) as cm:                           # nothing yet
            urllib.request.urlopen(self.url("/camera.jpg"), timeout=5)
        self.assertEqual(cm.exception.code, 404)
        req = urllib.request.Request(self.url("/camera"), data=jpg, headers={"Content-Type": "image/jpeg"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"bytes": len(jpg), "viewers": 0})
        with urllib.request.urlopen(self.url("/camera.jpg"), timeout=5) as r:
            self.assertEqual((r.headers["Content-Type"], r.read()), ("image/jpeg", jpg))
        stream = urllib.request.urlopen(self.url("/camera.mjpg"), timeout=5)
        self.streams.append(stream)
        self.assertIn("multipart/x-mixed-replace", stream.headers["Content-Type"])
        self.assertEqual(stream.readline(), b"--frame\r\n")
        stream.readline()
        stream.readline()
        stream.readline()
        self.assertEqual(stream.read(len(jpg)), jpg)
        for bad in (b"not a jpeg", b""):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(self.url("/camera"), data=bad, method="POST"), timeout=5)
            self.assertEqual(cm.exception.code, 400)

    def test_another_computer_may_watch_the_camera_but_not_post_to_it(self):
        from display.server import allowed
        self.assertTrue(allowed("GET", "/camera.mjpg", False))
        self.assertTrue(allowed("GET", "/camera.jpg", False))
        self.assertFalse(allowed("POST", "/camera", False))
        self.assertTrue(allowed("POST", "/camera", True))

    def test_the_operator_page_shows_the_camera_the_books_on_board_and_is_landscape(self):
        with urllib.request.urlopen(self.url("/operator"), timeout=5) as r:
            page = r.read().decode()
        for needle in ("/camera.mjpg", "Books on board", "grid-template-columns", "showBooks", "What can happen next", "showMap"):
            self.assertIn(needle, page)

    def test_the_robot_sends_to_the_display_from_a_worker_thread_so_it_never_waits(self):
        from robot.actuation.face import FaceDisplay
        import time
        c = self.listen()
        self.wait_for_clients(1)
        face = FaceDisplay("127.0.0.1", self.port, background=True)
        self.addCleanup(face._pool.shutdown)
        started = time.monotonic()
        self.assertTrue(face.show_screen(self.SCREEN))          # returns at once, before it is sent
        self.assertTrue(face.show_state({"state": "IDLE"}))
        face.flush()
        self.assertEqual(self.read_event(c), {"screen": self.SCREEN})            # in the order they were queued
        self.assertEqual(self.read_event(c), {"state": {"state": "IDLE"}})
        # a display that is not there: the call still returns at once and only the worker waits
        down = FaceDisplay("127.0.0.1", 1, background=True)
        self.addCleanup(down._pool.shutdown)
        t = time.monotonic()
        self.assertTrue(down.show_screen(self.SCREEN))
        self.assertLess(time.monotonic() - t, 0.1)
        down.flush()
        self.assertFalse(down.available)

    def test_the_inbox_keeps_only_the_newest_items(self):
        from display.server import INBOX_KEEP
        for i in range(INBOX_KEEP + 5):
            self.hub.inbox.add({"type": "choice", "choice": "done"})
        items = self.get_json("/inbox?after=0")["items"]
        self.assertEqual(len(items), INBOX_KEEP)
        self.assertEqual(items[-1]["id"], INBOX_KEEP + 5)


if __name__ == "__main__":
    unittest.main()
