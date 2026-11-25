"""Feedback while the robot works: thinking shows at once, the screen leads the speech (also inside a state), the state's
timeouts wait while it speaks or thinks, and the AI server's time to first byte is measured."""

import asyncio
import socket
import threading
import time
import unittest

from robot.events import (BookDropped, BookPlaced, PersonEngaged, ScreenUpdate, ThinkingEnded,
                          ThinkingStarted, UserUtterance)
from tests.test_dialogue import HITCH, StubClient, advance, run, scanned
from tests.test_fsm import started
from tests.test_state_machine import new_app, quiet_logs


class SlowClient(StubClient):
    def __init__(self, *replies, delay=0.5):
        super().__init__(*replies)
        self.delay = delay

    def chat(self, messages):
        time.sleep(self.delay)
        return super().chat(messages)


def watch_times(app):
    """[(seconds since start, what)] for screens, thinking, and the words as they are delivered."""
    t0, log = time.monotonic(), []
    stamp = lambda what: log.append((time.monotonic() - t0, what))
    app.bus.subscribe(ScreenUpdate, lambda e: stamp(("screen", e.view, e.thinking)))
    app.bus.subscribe(ThinkingStarted, lambda e: stamp(("thinking",)))
    app.bus.subscribe(ThinkingEnded, lambda e: stamp(("thinking_end",)))
    real = app.actuators.deliver

    def deliver(text, style):
        stamp(("spoken", text))
        return real(text, style)
    app.actuators.deliver = deliver
    return log


class ThinkingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def question(self, zone, delay=0.6, ack=0.15):
        app = new_app(zone, llm=SlowClient("It is about whales, mostly.", delay=delay))
        app.dialogue.settings.intents["thinking_ack_s"] = ack
        run(app, PersonEngaged())
        log = watch_times(app)
        run(app, UserUtterance("how big is a blue whale"))
        return app, log

    def test_thinking_shows_within_300_ms_in_both_zones(self):
        for zone in ("quiet", "common"):
            with self.subTest(zone=zone):
                app, log = self.question(zone)
                first_thinking = next(t for t, what in log if what[0] == "thinking")
                self.assertLess(first_thinking, 0.3)
                self.assertTrue(any(what[0] == "screen" and what[2] for _, what in log), "a screen with the thinking flag")

    def test_common_zone_says_let_me_check_once_when_the_answer_is_slow(self):
        app, log = self.question("common", delay=0.8, ack=0.15)
        fillers = [t for t, what in log if what == ("spoken", "Let me check.")]
        self.assertEqual(len(fillers), 1)
        answer = next(t for t, what in log if what[0] == "spoken" and "whales" in what[1])
        self.assertLess(fillers[0], answer)

    def test_a_quick_answer_gets_no_filler_and_the_quiet_zone_never_does(self):
        _, quick = self.question("common", delay=0.05, ack=0.5)
        self.assertFalse([w for _, w in quick if w == ("spoken", "Let me check.")])
        _, quiet = self.question("quiet", delay=0.8, ack=0.15)
        self.assertFalse([w for _, w in quiet if w == ("spoken", "Let me check.")])

    def test_thinking_ends_when_the_reply_comes(self):
        app, log = self.question("common", delay=0.3, ack=1.5)
        kinds = [w[0] for _, w in log]
        self.assertLess(kinds.index("thinking"), kinds.index("thinking_end"))
        self.assertLess(kinds.index("thinking_end"), kinds.index("spoken"))


class ScreenBeforeSpeechInStateTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def slow_speech(self, app, log):
        real = app.actuators.deliver
        app.bus.subscribe(ScreenUpdate, lambda e: log.append(("screen", e.view)))

        def deliver(text, style):
            log.append(("spoken", text))
            time.sleep(0.05)
            return real(text, style)
        app.actuators.deliver = deliver

    def test_the_drop_confirmation_is_on_the_screen_before_it_is_spoken(self):
        for zone in ("quiet", "common"):
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged(), BookPlaced(), scanned(HITCH), UserUtterance("I want to return it"))
                log = []
                self.slow_speech(app, log)
                run(app, BookDropped())
                spoken = next(i for i, e in enumerate(log) if e[0] == "spoken" and "storage slot" in e[1])
                self.assertTrue(any(e[0] == "screen" for e in log[:spoken]), log)

    def test_small_talk_replies_are_on_the_screen_before_they_are_spoken(self):
        app = new_app("common", llm=StubClient("Nice to meet you too."))
        run(app, PersonEngaged())
        log = []
        self.slow_speech(app, log)
        run(app, UserUtterance("what a lovely day"))
        spoken = next(i for i, e in enumerate(log) if e[0] == "spoken")
        self.assertTrue(any(e[0] == "screen" for e in log[:spoken]), log)


class TimeoutsWaitTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_fsm_holds_timeouts_while_suspended_and_restarts_them_after(self):
        async def go():
            machine, handler, clock = await started()
            await machine.dispatch("go")
            await machine.dispatch("next")                      # B2 has a 6 s timeout
            machine.suspend_timeouts()
            clock.now += 100
            self.assertEqual(await machine.tick(), 0)
            self.assertEqual(machine.path, "B.B2")
            machine.resume_timeouts()
            clock.now += 5.9
            self.assertEqual(await machine.tick(), 0)           # the full 6 s again, from the moment it resumed
            clock.now += 0.2
            self.assertEqual(await machine.tick(), 1)
            self.assertEqual(machine.path, "C")
        asyncio.run(go())

    def test_nested_holds_release_only_with_the_last_one(self):
        async def go():
            machine, handler, clock = await started()
            await machine.dispatch("go")
            await machine.dispatch("next")
            machine.suspend_timeouts()
            machine.suspend_timeouts()
            machine.resume_timeouts()
            clock.now += 100
            self.assertEqual(await machine.tick(), 0)
            machine.resume_timeouts()
            clock.now += 7
            self.assertEqual(await machine.tick(), 1)
        asyncio.run(go())

    def test_a_slow_llm_answer_does_not_let_the_offer_time_out(self):
        app = new_app("common", llm=SlowClient("It is about whales.", delay=0.5))
        app.dialogue.settings.timeouts["enabled"] = True
        run(app, PersonEngaged())
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

        async def go():
            question = asyncio.create_task(app.bus.publish(UserUtterance("how big is a blue whale")))
            await asyncio.sleep(0.1)
            app.now[0] += 200                                    # far past the offer's timeout, while the LLM thinks
            await app.dialogue.tick()
            self.assertEqual([m for m in app.moves if m.event == "timeout"], [])
            await question
        asyncio.run(go())
        timeouts = lambda: [m for m in app.moves if m.event == "timeout"]
        self.assertEqual(timeouts(), [])
        advance(app, 8)                                          # the timer starts again after the answer, from full
        self.assertEqual(timeouts(), [])
        advance(app, 40)
        self.assertTrue(timeouts())

    def test_the_timeout_waits_while_the_robot_speaks(self):
        app = new_app("common")
        app.dialogue.settings.timeouts["enabled"] = True
        real = app.actuators.deliver
        seen = []

        def deliver(text, style):
            seen.append(app.dialogue.fsm._suspended)
            return real(text, style)
        app.actuators.deliver = deliver
        run(app, PersonEngaged())
        self.assertTrue(seen and all(n > 0 for n in seen), seen)
        self.assertEqual(app.dialogue.fsm._suspended, 0)


class LLMTimingTest(unittest.TestCase):
    def serve(self, delay_before):
        """A one-shot server speaking the vendor's framing: it reads the request, waits, then sends the whole reply."""
        from vendor.ai_server.llm import receive_json, send_json
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def handle():
            conn, _ = server.accept()
            with conn:
                request = receive_json(conn)
                time.sleep(delay_before)
                send_json(conn, {"robot_id": request["robot_id"], "request_id": request["request_id"], "command": [],
                                 "parameters": {}, "content": "hello there"})
            server.close()
        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        return server.getsockname()[1], thread

    def test_first_byte_and_total_are_measured_and_close_for_a_reply_sent_whole(self):
        from robot.services import llm
        port, thread = self.serve(0.3)
        reply, timing = llm.timed_ask("127.0.0.1", port, "hi", timeout=5)
        thread.join(2)
        self.assertEqual(reply, "hello there")
        self.assertGreaterEqual(timing["first_byte_ms"], 250)
        self.assertGreaterEqual(timing["total_ms"], timing["first_byte_ms"])
        self.assertLess(timing["total_ms"] - timing["first_byte_ms"], 150)


class GreetingHoldTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def hello_then_options(self, zone, hold, speech_seconds=0.0):
        app = new_app(zone)
        app.dialogue.settings.screen["greeting_hold_s"] = hold
        real = app.actuators.deliver

        def deliver(text, style):
            time.sleep(speech_seconds)
            return real(text, style)
        app.actuators.deliver = deliver
        log, t0 = [], time.monotonic()
        app.bus.subscribe(ScreenUpdate, lambda e: log.append((time.monotonic() - t0, e.view, e.state_path)))
        run(app, PersonEngaged())
        face = next(t for t, view, path in log if path == "ENGAGED.GREET")
        options = next(t for t, view, path in log if view == "options")
        return face, options

    def test_the_hello_stays_on_the_face_before_the_options_appear(self):
        for zone in ("quiet", "common"):
            with self.subTest(zone=zone):
                face, options = self.hello_then_options(zone, 0.4)
                self.assertGreaterEqual(options - face, 0.38)

    def test_a_hello_that_takes_longer_than_the_hold_is_not_delayed_further(self):
        face, options = self.hello_then_options("common", 0.2, speech_seconds=0.5)
        self.assertLess(options - face, 0.8)

    def test_no_hold_when_it_is_zero(self):
        face, options = self.hello_then_options("common", 0)
        self.assertLess(options - face, 0.2)


class SpeechCheckTest(unittest.TestCase):
    def test_an_unreachable_tts_server_is_reported_with_where_to_fix_it(self):
        from robot.services.speech import Speech
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()                                           # nothing listens on this port now
        problem = Speech("127.0.0.1", port).check(1.0)
        self.assertIn(str(port), problem)
        self.assertIn("tts_port", problem)

    def test_a_reachable_server_is_fine(self):
        from robot.services.speech import Speech
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            self.assertIsNone(Speech("127.0.0.1", server.getsockname()[1]).check(1.0))
        finally:
            server.close()
