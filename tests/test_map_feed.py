"""robot/map_feed.py: the base's map goes to the display server, and again when that was restarted."""

import base64
import json
import threading
import unittest

from display.server import Hub, make_server
from robot.map_feed import MapFeed

PNG = b"\x89PNG\r\n\x1a\n" + b"data"


def image(version, png=PNG):
    return {"changed": True, "version": version, "resolution": 0.05, "width": 4, "height": 3, "origin": {"x": 0.0, "y": 0.0}, "frame": "map", "png": png}


class ScriptedLink:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def fetch_map(self):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class MapFeedTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.server = make_server(self.hub, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_new_map_is_handed_over_once(self):
        feed = MapFeed(ScriptedLink(image(5), {"changed": False, "version": 5}), "127.0.0.1", self.port)
        self.assertTrue(feed.step())
        self.assertEqual((self.hub.map_meta["version"], self.hub.map_png), (5, PNG))
        self.assertFalse(feed.step(), "nothing changed: nothing sent again")

    def test_no_map_from_the_base_sends_nothing(self):
        feed = MapFeed(ScriptedLink(None), "127.0.0.1", self.port)
        self.assertFalse(feed.step())
        self.assertIsNone(self.hub.map_png)

    def test_a_restarted_display_server_gets_the_map_again(self):
        feed = MapFeed(ScriptedLink(image(5), {"changed": False, "version": 5}), "127.0.0.1", self.port)
        feed.step()
        self.hub.map_meta = self.hub.map_png = None                      # the display server lost it
        self.assertTrue(feed.step())
        self.assertEqual(self.hub.map_meta["version"], 5)

    def test_a_changed_map_replaces_the_old_one(self):
        feed = MapFeed(ScriptedLink(image(5), image(6, PNG + b"2")), "127.0.0.1", self.port)
        feed.step()
        feed.step()
        self.assertEqual((self.hub.map_meta["version"], self.hub.map_png), (6, PNG + b"2"))

    def test_no_display_server_is_not_an_error(self):
        self.server.shutdown()
        feed = MapFeed(ScriptedLink(image(5)), "127.0.0.1", self.port)
        self.assertFalse(feed.step())


if __name__ == "__main__":
    unittest.main()


class FakeFace:
    def __init__(self, ok=True):
        self.ok, self.shown = ok, []

    def show_state(self, info):
        self.shown.append(info)
        return self.ok


class FakeApp:
    def __init__(self, face):
        self.actuators = type("A", (), {"face": face})()
        self.link = {"status": "connected", "localised": True, "battery": 0.5, "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
                     "nav": {"status": "idle", "goal_id": "", "distance_left": None}}

    def state_info(self):
        return {"state": "IDLE", "link": self.link}


class StatePushTest(unittest.TestCase):
    def push(self, app):
        from robot.map_feed import StatePush
        push = StatePush(app)
        due = push.pending()
        return push, due, (push.send(*due) if due else None)

    def test_the_pose_is_sent_once_and_again_only_when_it_changes(self):
        face = FakeFace()
        app = FakeApp(face)
        push, due, sent = self.push(app)
        self.assertTrue(sent)
        self.assertEqual(len(face.shown), 1)
        self.assertIsNone(push.pending(), "nothing changed: nothing to send")
        app.link["pose"] = {"x": 1.004, "y": 2.0, "yaw": 0.5}
        self.assertIsNone(push.pending(), "less than a centimetre is not a change")
        app.link["pose"] = {"x": 1.3, "y": 2.0, "yaw": 0.5}
        self.assertIsNotNone(push.pending())

    def test_a_goal_or_a_lost_link_is_a_change(self):
        app = FakeApp(FakeFace())
        push, _, _ = self.push(app)
        app.link["nav"] = {"status": "active", "goal_id": "g1", "distance_left": 1.9}
        self.assertIsNotNone(push.pending())
        push.send(*push.pending())
        app.link["status"] = "lost"
        self.assertIsNotNone(push.pending())

    def test_a_display_server_that_was_away_gets_it_again(self):
        face = FakeFace(ok=False)
        push, due, sent = self.push(FakeApp(face))
        self.assertFalse(sent)
        self.assertIsNotNone(push.pending(), "not taken: try again")
        face.ok = True
        self.assertTrue(push.send(*push.pending()))
        self.assertIsNone(push.pending())
