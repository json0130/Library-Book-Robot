import asyncio
import unittest

from robot.bus import Bus
from robot.events import BookScanned, UserUtterance


def run(coro):
    return asyncio.run(coro)


class BusTest(unittest.TestCase):
    def test_handlers_run_in_subscription_order(self):
        bus, seen = Bus(), []
        bus.subscribe(UserUtterance, lambda e: seen.append(("a", e.text)))
        bus.subscribe(UserUtterance, lambda e: seen.append(("b", e.text)))
        run(bus.publish(UserUtterance("hi")))
        run(bus.publish(UserUtterance("again")))
        self.assertEqual(seen, [("a", "hi"), ("b", "hi"), ("a", "again"), ("b", "again")])

    def test_async_handlers_are_awaited_in_order(self):
        bus, seen = Bus(), []

        async def slow(e):
            await asyncio.sleep(0.01)
            seen.append("slow")

        bus.subscribe(UserUtterance, slow)
        bus.subscribe(UserUtterance, lambda e: seen.append("sync"))
        self.assertEqual(run(bus.publish(UserUtterance("x"))), 2)
        self.assertEqual(seen, ["slow", "sync"])

    def test_only_matching_types_and_base_class_subscribers(self):
        bus, seen = Bus(), []
        bus.subscribe(BookScanned, lambda e: seen.append("book"))
        bus.subscribe(object, lambda e: seen.append(type(e).__name__))
        run(bus.publish(UserUtterance("x")))
        run(bus.publish(BookScanned("1")))
        self.assertEqual(seen, ["UserUtterance", "book", "BookScanned"])

    def test_failing_handler_does_not_stop_others(self):
        bus, seen = Bus(), []

        def broken(e):
            raise RuntimeError("boom")

        async def broken_async(e):
            raise ValueError("async boom")

        bus.subscribe(UserUtterance, broken)
        bus.subscribe(UserUtterance, broken_async)
        bus.subscribe(UserUtterance, lambda e: seen.append(e.text))
        with self.assertLogs("robot.bus", level="ERROR"):
            ok = run(bus.publish(UserUtterance("still delivered")))
        self.assertEqual(ok, 1)
        self.assertEqual(seen, ["still delivered"])
        self.assertEqual([type(err).__name__ for _, _, err in bus.errors], ["RuntimeError", "ValueError"])

    def test_handler_can_publish_further_events(self):
        bus, seen = Bus(), []

        async def echo(e):
            seen.append(e.text)
            if e.text == "first":
                await bus.publish(UserUtterance("second"))

        bus.subscribe(UserUtterance, echo)
        run(bus.publish(UserUtterance("first")))
        self.assertEqual(seen, ["first", "second"])

    def test_catch_all_keeps_its_place_in_the_order(self):
        bus, seen = Bus(), []
        bus.subscribe(object, lambda e: seen.append(f"log {e.text}"))       # e.g. the session log

        async def reply(e):
            if e.text == "question":
                await bus.publish(UserUtterance("answer"))

        bus.subscribe(UserUtterance, reply)
        run(bus.publish(UserUtterance("question")))
        self.assertEqual(seen, ["log question", "log answer"])               # cause before effect

    def test_unsubscribe(self):
        bus, seen = Bus(), []
        h = lambda e: seen.append(1)  # noqa: E731
        bus.subscribe(UserUtterance, h)
        bus.unsubscribe(UserUtterance, h)
        run(bus.publish(UserUtterance("x")))
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
