"""LLM warm-up and keep-alive (robot/services/warmup.py): silent, in the background, only while idle, with the server mocked."""

import asyncio
import logging
import threading
import time
import unittest

from robot.events import UserUtterance
from robot.scenarios import runner
from robot.services.llm import LLMClient, LLMError
from robot.services.warmup import MESSAGES, Warmup
from tests.test_dialogue import run
from tests.test_state_machine import new_app, quiet_logs


class FakeServer:
    """Stands in for the LLM client: records when it was asked, answers after `delay`, or fails."""

    def __init__(self, delay=0.0, error=None):
        self.delay, self.error, self.calls, self.threads = delay, error, [], []

    def chat(self, messages):
        self.calls.append((time.monotonic(), messages))
        self.threads.append(threading.current_thread())
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return "ready"


async def running(warmup, seconds):
    task = asyncio.create_task(warmup.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class WarmupTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_one_warm_up_request_then_nothing_when_keepalive_is_off(self):
        server = FakeServer()
        warmup = Warmup(server, lambda: True, keepalive_s=0)
        asyncio.run(running(warmup, 0.3))
        self.assertEqual(len(server.calls), 1)
        self.assertEqual([k for k, _ in warmup.latencies], ["warm-up"])

    def test_the_request_is_tiny(self):
        server = FakeServer()
        asyncio.run(running(Warmup(server, lambda: True, 0), 0.1))
        messages = server.calls[0][1]
        self.assertEqual(messages, MESSAGES)
        self.assertLess(len(messages[-1]["content"]), 60)

    def test_start_up_is_not_blocked_by_a_slow_warm_up(self):
        server = FakeServer(delay=0.6)

        async def go():
            task = asyncio.create_task(Warmup(server, lambda: True, 0).run())
            started = time.monotonic()
            await asyncio.sleep(0)                            # the rest of start-up carries on at once
            await asyncio.sleep(0.05)
            free = time.monotonic() - started
            task.cancel()
            return free
        self.assertLess(asyncio.run(go()), 0.3)
        self.assertNotEqual(server.threads[0], threading.main_thread())      # the call is off the event loop

    def test_keepalives_repeat_while_idle(self):
        server = FakeServer()
        warmup = Warmup(server, lambda: True, keepalive_s=0.1)
        asyncio.run(running(warmup, 0.55))
        kinds = [k for k, _ in warmup.latencies]
        self.assertEqual(kinds[0], "warm-up")
        self.assertGreaterEqual(kinds.count("keep-alive"), 3)
        gaps = [b[0] - a[0] for a, b in zip(server.calls[1:], server.calls[2:])]
        self.assertTrue(all(g >= 0.09 for g in gaps), gaps)

    def test_no_keepalive_while_the_robot_is_busy(self):
        server, idle = FakeServer(), {"now": False}
        warmup = Warmup(server, lambda: idle["now"], keepalive_s=0.1)

        async def go():
            task = asyncio.create_task(warmup.run())
            await asyncio.sleep(0.45)
            busy_calls = len(server.calls)
            idle["now"] = True
            await asyncio.sleep(0.4)
            task.cancel()
            return busy_calls
        busy_calls = asyncio.run(go())
        self.assertEqual(busy_calls, 1)                       # only the warm-up
        self.assertGreater(len(server.calls), 1)              # and a keep-alive once idle again

    def test_latencies_are_logged_and_recorded(self):
        server = FakeServer(delay=0.05)
        warmup = Warmup(server, lambda: True, keepalive_s=0)
        with self.assertLogs("robot.warmup", level=logging.INFO) as logged:
            logging.disable(logging.NOTSET)
            asyncio.run(running(warmup, 0.3))
        self.assertTrue(any("warm-up answered in" in line and " ms" in line for line in logged.output), logged.output)
        kind, ms = warmup.latencies[0]
        self.assertGreaterEqual(ms, 50)

    def test_a_failing_server_is_logged_and_never_raised(self):
        server = FakeServer(error=LLMError("Could not reach the AI server"))
        warmup = Warmup(server, lambda: True, keepalive_s=0.1)
        with self.assertLogs("robot.warmup", level=logging.WARNING) as logged:
            logging.disable(logging.NOTSET)
            asyncio.run(running(warmup, 0.35))
        self.assertGreaterEqual(len(warmup.latencies), 3)     # it keeps trying: the server may come up later
        self.assertTrue(all(ms is None for _, ms in warmup.latencies))
        self.assertTrue(any("Could not reach" in line for line in logged.output))

    def test_an_unexpected_error_does_not_end_the_loop(self):
        server = FakeServer(error=RuntimeError("boom"))
        warmup = Warmup(server, lambda: True, keepalive_s=0.1)
        logging.disable(logging.CRITICAL)
        asyncio.run(running(warmup, 0.3))
        self.assertGreaterEqual(len(server.calls), 2)


class FromAppTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_config_sets_the_interval(self):
        app = new_app()
        self.assertEqual(Warmup.from_app(app).keepalive_s, 240)                # the default in config/robot.yaml
        app.cfg.robot["ai_server"]["keepalive_s"] = 90
        self.assertEqual(Warmup.from_app(app).keepalive_s, 90)
        app.cfg.robot["ai_server"]["keepalive_s"] = 0
        self.assertEqual(Warmup.from_app(app).keepalive_s, 0)
        del app.cfg.robot["ai_server"]["keepalive_s"]
        self.assertEqual(Warmup.from_app(app).keepalive_s, 240)                # not set: the default

    def test_idle_means_the_dialogue_is_idle(self):
        app = new_app()
        warmup = Warmup.from_app(app)
        self.assertTrue(warmup.is_idle())
        run(app, UserUtterance("hello"))                                       # speech means a person is here
        self.assertFalse(warmup.is_idle())

    def test_it_uses_its_own_client(self):
        app = new_app()
        self.assertIsInstance(Warmup.from_app(app).client, LLMClient)          # not the assistant's: last_timing stays the visitor's

    def test_it_says_and_shows_nothing(self):
        """Against the repo's mock AI server: the requests are made, and the bus and the actuators see nothing of them."""
        app = new_app()
        events, spoken = [], []
        app.bus.subscribe(object, events.append)
        deliver = app.actuators.deliver
        app.actuators.deliver = lambda text, style: (spoken.append(text), deliver(text, style))[1]
        shown = []
        app.actuators.face.show_screen = lambda update: shown.append(update)
        with runner.mock_llm_server() as (host, port):
            app.cfg.robot["ai_server"].update(host=host, port=port, keepalive_s=0.1)
            warmup = Warmup.from_app(app)
            asyncio.run(running(warmup, 0.6))
        kinds = [k for k, _ in warmup.latencies]
        self.assertEqual(kinds[0], "warm-up")
        self.assertIn("keep-alive", kinds)
        self.assertTrue(all(ms is not None for _, ms in warmup.latencies), warmup.latencies)
        self.assertEqual((events, spoken, shown), ([], [], []))
        self.assertEqual(app.dialogue.leaf, "IDLE")


if __name__ == "__main__":
    unittest.main()
