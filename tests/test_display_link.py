import asyncio
import logging
import threading
import unittest
import urllib.request
import json

from display.server import Hub, make_server
from robot.actuation.face import FaceDisplay
from robot.app import build_app, fake_drivers
from robot.config import load_config
from robot.display_link import DisplayLink
from robot.events import PersonEngaged, UserChoice
from robot.operator import Operator


class DisplayLinkTest(unittest.TestCase):
    """The robot and a real display server talking to each other over HTTP."""

    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.hub = Hub()
        self.server = make_server(self.hub, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        self.app = build_app(cfg, fake_drivers(), face=FaceDisplay("127.0.0.1", self.port), log_sessions=False,
                             persist_storage=False)
        self.link = DisplayLink(self.app.bus, Operator(self.app), "127.0.0.1", self.port, 0.05)

    def tap(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).close()

    def test_a_screen_update_from_the_robot_reaches_the_event_stream(self):
        events = self.hub.subscribe()

        async def go():
            await self.app.bus.publish(PersonEngaged())

        asyncio.run(go())
        seen = []
        while not events.empty():
            seen.append(events.get_nowait())
        screens = [e["screen"] for e in seen if "screen" in e]
        states = [e["state"] for e in seen if "state" in e]
        self.assertIn("ENGAGED.OFFER", [s["state_path"] for s in screens])
        self.assertEqual(states[-1]["state"], "ENGAGED.OFFER")
        self.assertEqual(states[-1]["zone"], "common")
        self.assertIn("OFFER", states[-1]["states"])                                  # the operator page's dropdown

    def test_taps_and_operator_commands_come_back_as_events_after_the_first_poll(self):
        chosen = []
        self.app.bus.subscribe(UserChoice, chosen.append)
        self.tap("/input", {"choice": "recommend"})                                    # before the robot looked: not replayed

        async def go():
            self.assertEqual(await self.link.step(), 0)                                # the first poll only finds the end
            self.tap("/input", {"choice": "return"})
            self.tap("/operator", {"command": "engage"})
            self.tap("/operator", {"command": "pick", "arg": "ask"})
            self.assertEqual(await self.link.step(), 3)
            self.assertEqual(await self.link.step(), 0)                                # nothing twice

        asyncio.run(go())
        self.assertEqual([(c.choice, c.source) for c in chosen], [("return", "screen"), ("ask", "operator")])
        self.assertEqual(self.app.dialogue.state, "ENGAGED.AWAIT_BOOK")

    def test_a_missing_display_server_is_not_an_error(self):
        link = DisplayLink(self.app.bus, Operator(self.app), "127.0.0.1", 1, 0.05)     # nothing listens on port 1
        self.assertEqual(asyncio.run(link.step()), -1)
        self.assertFalse(link.connected)

    def test_a_bad_item_does_not_stop_the_link(self):
        async def go():
            await self.link.step()
            self.hub.inbox.add({"type": "mystery"})
            self.hub.inbox.add({"type": "operator", "command": "goto", "arg": "nowhere"})
            self.assertEqual(await self.link.step(), 2)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
