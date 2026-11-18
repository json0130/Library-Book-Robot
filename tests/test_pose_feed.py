"""display/pose_feed.py: the read-only live position of the robot for the operator page's floor plan, and /pose.json."""

import os
import stat
import tempfile
import unittest
import urllib.error

from display.pose_feed import PoseFeed, token_from_env
from display.server import allowed


def state(x, y, yaw=0.0, localised=True, nav=None):
    return {"pose": {"x": x, "y": y, "yaw": yaw, "frame": "map", "localised": localised, "t": 1.0},
            "nav": nav or {"status": "idle", "goal_id": "", "distance_left": None, "reason": ""}, "noise": None, "time": 2.0}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class PoseFeedTest(unittest.TestCase):
    def feed(self, *answers, token="secret"):
        answers = list(answers)
        clock = Clock()

        def fetch():
            a = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(a, Exception):
                raise a
            return a
        return PoseFeed("http://x", token, clock=clock, fetch=fetch), clock

    def test_the_pose_and_nav_status_come_through_and_the_trail_grows_only_when_it_moves(self):
        f, clock = self.feed(state(0, 0), state(0.01, 0.0), state(0.3, 0.0, 1.5708, nav={"status": "active", "goal_id": "g", "distance_left": 2.5, "reason": ""}))
        for _ in range(3):
            f.poll_once()
        snap = f.snapshot()
        self.assertTrue(snap["available"])
        self.assertEqual((snap["pose"]["x"], snap["pose"]["yaw"]), (0.3, 1.5708))
        self.assertEqual(snap["nav"], {"status": "active", "distance_left": 2.5, "reason": ""})
        self.assertEqual(snap["trail"], [[0, 0], [0.3, 0.0]])               # the 1 cm step is not a new trail point

    def test_the_trail_is_capped_and_can_be_cleared(self):
        f, _ = self.feed(*[state(i * 0.1, 0) for i in range(450)])
        for _ in range(450):
            f.poll_once()
        self.assertEqual(len(f.snapshot()["trail"]), 400)
        f.clear_trail()
        self.assertEqual(f.snapshot()["trail"], [])

    def test_no_token_means_nothing_is_read_and_the_page_is_told_why(self):
        f, _ = self.feed(state(1, 1), token="")
        f.poll_once()
        snap = f.snapshot()
        self.assertFalse(snap["available"])
        self.assertIn("ROBOT_LINK_TOKEN", snap["reason"])

    def test_an_unreachable_or_refusing_base_and_a_stale_answer_are_reported_not_shown(self):
        f, clock = self.feed(state(1, 1), OSError("no route"))
        f.poll_once()
        self.assertTrue(f.snapshot()["available"])
        f.poll_once()                                                          # the tunnel dropped
        snap = f.snapshot()
        self.assertFalse(snap["available"])
        self.assertIn("tunnel", snap["reason"])
        f2, clock2 = self.feed(state(1, 1))
        f2.poll_once()
        clock2.t += 6                                                          # nothing newer for 6 s
        self.assertFalse(f2.snapshot()["available"])
        f3, _ = self.feed(urllib.error.HTTPError("http://x", 401, "no", {}, None))
        f3.poll_once()
        self.assertIn("refused", f3.snapshot()["reason"])
        self.assertNotIn("secret", f3.snapshot()["reason"])
        f4, _ = self.feed({"nav": {}})
        f4.poll_once()
        self.assertIn("no pose", f4.snapshot()["reason"])

    def test_the_token_comes_from_the_environment_or_a_private_file_only(self):
        self.assertEqual(token_from_env({"ROBOT_LINK_TOKEN": " abc "}), "abc")
        self.assertEqual(token_from_env({}), "")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tok")
            with open(path, "w") as fh:
                fh.write("filetoken\n")
            os.chmod(path, 0o600)
            self.assertEqual(token_from_env({"ROBOT_LINK_TOKEN_FILE": path}), "filetoken")
            os.chmod(path, 0o644)                                              # readable by others: not used
            self.assertEqual(token_from_env({"ROBOT_LINK_TOKEN_FILE": path}), "")

    def test_the_feed_only_reads_and_another_computer_may_read_it_but_not_write_it(self):
        self.assertTrue(allowed("GET", "/pose.json", local=False))
        self.assertFalse(allowed("POST", "/pose.json", local=False))
        import inspect
        import display.pose_feed as module
        source = inspect.getsource(module)
        self.assertIn('method="GET"', source)
        for word in ("/api/goal", "/api/heartbeat", "/api/max_speed", "/api/cancel", "/api/speaking"):
            self.assertNotIn(word, source.split('"""', 2)[2])                  # nothing here can make the base move


if __name__ == "__main__":
    unittest.main()
