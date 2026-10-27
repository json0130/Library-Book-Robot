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


if __name__ == "__main__":
    unittest.main()
