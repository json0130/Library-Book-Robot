import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from robot.actuation.face import FaceDisplay
from robot.app import build_app, fake_drivers
from robot.config import load_config
from robot.core.session_log import SessionLog
from robot.core.dialogue import SCAN_FAILED_MESSAGE, choice_from
from robot.events import (BookPlaced, BookScanned, ContextChanged, EmotionDetected, ExpressionRequested,
                          ReplySentence, ScanFailed, StyleChanged, UserUtterance)

HITCH, MARTIAN, CLEAN_CODE = "9780345391803", "9780553418026", "9780132350884"


def make_app(log_dir=None):
    cfg = load_config()
    cfg.robot.setdefault("books", {})["online_fallback"] = False      # tests never touch the network
    app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False)
    if log_dir is not None:
        app.session_log = SessionLog(log_dir, "test")
    return app


def run(app, *events):
    async def go():
        for e in events:
            await app.bus.publish(e)
    asyncio.run(go())


class DialogueTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)          # actuator logs are noise here
        self.addCleanup(logging.disable, logging.NOTSET)

    def path(self, app):
        return [t[2] for t in app.dialogue.transitions]

    def test_full_book_flow(self):
        app = make_app()
        run(app, ContextChanged("quiet", 1), BookPlaced(), BookScanned("1", "Dune"),
            UserUtterance("I want to return it"), UserUtterance("no thanks, bye"))
        self.assertEqual(self.path(app), ["book_placed", "scanning", "offering", "acting", "closing", "closing"])
        self.assertIn("chose return", [t[1] for t in app.dialogue.transitions])
        self.assertEqual(app.dialogue.book.title, "Dune")

    def test_proactive_invite_only_in_common_areas(self):
        quiet = make_app()
        run(quiet, ContextChanged("quiet", 1))
        self.assertEqual(quiet.dialogue.state, "idle")
        common = make_app()
        run(common, ContextChanged("common", 2))
        self.assertEqual(common.dialogue.state, "invited")

    def test_visitor_can_start_and_leaving_resets(self):
        app = make_app()
        run(app, ContextChanged("quiet", 1), UserUtterance("hello"))
        self.assertEqual(app.dialogue.state, "invited")
        run(app, BookPlaced())
        self.assertEqual(app.dialogue.state, "scanning")
        run(app, ContextChanged("quiet", 0))
        self.assertEqual(app.dialogue.state, "idle")
        self.assertIsNone(app.dialogue.book)

    def test_scan_result_ignored_unless_scanning(self):
        app = make_app()
        run(app, BookScanned("1", "Dune"))
        self.assertEqual(app.dialogue.state, "idle")

    def test_actions_follow_the_style(self):
        quiet, common = make_app(), make_app()
        run(quiet, ContextChanged("quiet", 1), BookPlaced(), BookScanned("1", "Dune"))
        run(common, ContextChanged("common", 1), BookPlaced(), BookScanned("1", "Dune"))
        quiet_cmds = [c for c, _ in quiet.actuators.lights.esp32.sent]
        self.assertIn("print", quiet_cmds)                  # quiet+alone: printed slip, no speech
        common_cmds = [c for c, _ in common.actuators.lights.esp32.sent]
        self.assertNotIn("print", common_cmds)              # common: spoken and on screen instead


def scanned(isbn):
    """A BookScanned for a catalogue book, as the scanner would publish it."""
    app = make_app()
    book = app.finder.find(isbn)
    return BookScanned(isbn=isbn, title=book.title, author=book.author, summary=book.summary,
                       shelf=book.shelf, slot=book.slot, found_in_catalog=True)


class BookChoicesTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def offer(self, zone="common", isbn=HITCH, event=None):
        """An app that has just offered the three choices for a book."""
        app = make_app()
        app.said = []
        app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
        run(app, ContextChanged(zone, 1), BookPlaced(), event or scanned(isbn))
        self.assertEqual(app.dialogue.state, "offering")
        return app

    def test_choice_words(self):
        self.assertEqual(choice_from("I'd like to return it"), "return")
        self.assertEqual(choice_from("Explain"), "explain")
        self.assertEqual(choice_from("what is it about?"), "explain")
        self.assertEqual(choice_from("Recommend something"), "recommend")
        self.assertEqual(choice_from("anything similar?"), "recommend")
        self.assertIsNone(choice_from("hmm, maybe"))

    def test_offer_names_the_book_and_the_three_choices(self):
        app = self.offer()
        self.assertIn("Hitchhiker's Guide", app.said[-1])
        self.assertIn("Douglas Adams", app.said[-1])
        for word in ("return", "hear about", "recommendation"):
            self.assertIn(word, app.said[-1])

    def test_return_gives_shelf_and_slot_and_turns_the_ring(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("return"))
        said = " ".join(app.said)
        self.assertIn("storage slot 4", said)
        self.assertIn("shelf Level 2 SF-WEI", said)
        self.assertIn(("ring", {"slot": 4}), app.actuators.ring.esp32.sent)
        self.assertEqual(app.dialogue.state, "closing")

    def test_return_of_a_book_not_in_the_catalogue(self):
        unknown = BookScanned(isbn="9780140449136", title="Some Online Book", found_in_catalog=False)
        app = self.offer(event=unknown)
        run(app, UserUtterance("return"))
        self.assertIn("isn't in our catalogue", app.said[-2])
        self.assertNotIn("ring", [c for c, _ in app.actuators.ring.esp32.sent])

    def test_explain_gives_the_catalogue_summary(self):
        app = self.offer()
        run(app, UserUtterance("explain"))
        self.assertIn("whisked off Earth", app.said[-2])
        self.assertEqual(app.dialogue.state, "closing")

    def test_explain_without_a_summary(self):
        app = self.offer(event=BookScanned(isbn="9780140449136", title="Online Only"))
        run(app, UserUtterance("tell me about it"))
        self.assertIn("don't have a summary for Online Only", app.said[-2])

    def test_recommend_names_up_to_three_similar_books(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("recommend"))
        said = app.said[-2]
        self.assertIn("If you like The Martian", said)
        expected = [b.title for b in app.finder.similar_to(MARTIAN, 3)]
        self.assertEqual(len(expected), 3)
        for title in expected:
            self.assertIn(title, said)
        self.assertNotIn("The Martian by", said)               # never recommends the book itself

    def test_recommend_edge_cases(self):
        app = self.offer(isbn=CLEAN_CODE)                       # shares no tags with anything
        run(app, UserUtterance("recommend"))
        self.assertIn("couldn't find books like Clean Code", app.said[-2])
        app = self.offer(event=BookScanned(isbn="9780140449136", title="Online Only"))
        run(app, UserUtterance("recommend"))
        self.assertIn("only recommend from books in our catalogue", app.said[-2])

    def test_unclear_answer_repeats_the_offer_and_stays(self):
        app = self.offer()
        run(app, UserUtterance("hmm, I don't know"))
        self.assertEqual(app.dialogue.state, "offering")
        self.assertIn("return it, hear about it", app.said[-1])
        run(app, UserUtterance("explain"))
        self.assertEqual(app.dialogue.state, "closing")

    def test_output_follows_the_style_channels(self):
        quiet = self.offer(zone="quiet", isbn=MARTIAN)
        run(quiet, UserUtterance("explain"))
        printed = [p["text"] for c, p in quiet.actuators.lights.esp32.sent if c == "print"]
        self.assertTrue(any("Mars" in t for t in printed))      # quiet+alone prints, never speaks
        common = self.offer(zone="common", isbn=MARTIAN)
        run(common, UserUtterance("explain"))
        self.assertNotIn("print", [c for c, _ in common.actuators.lights.esp32.sent])

    def test_scan_failed_returns_to_idle_and_asks_for_a_flat_book(self):
        app = make_app()
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        run(app, ContextChanged("quiet", 1), BookPlaced())
        self.assertEqual(app.dialogue.state, "scanning")
        run(app, ScanFailed("no ISBN barcode found within 5 s"))
        self.assertEqual(app.dialogue.state, "idle")
        self.assertEqual(said, [SCAN_FAILED_MESSAGE])
        self.assertIn("flat", SCAN_FAILED_MESSAGE)
        self.assertIn("barcode facing up", SCAN_FAILED_MESSAGE)
        sent = app.actuators.lights.esp32.sent
        self.assertIn(("spotlight", {"on": False}), sent)
        self.assertIn("print", [c for c, _ in sent])            # quiet+alone: through the print channel

    def test_scan_failed_in_a_common_area_is_spoken(self):
        app = make_app()
        run(app, ContextChanged("common", 1), BookPlaced(), ScanFailed("x"))
        self.assertNotIn("print", [c for c, _ in app.actuators.lights.esp32.sent])
        self.assertEqual(app.dialogue.state, "idle")

    def test_scan_failed_is_ignored_unless_scanning(self):
        app = self.offer()
        run(app, ScanFailed("late"))
        self.assertEqual(app.dialogue.state, "offering")

    def test_bus_flow_from_placement_through_the_scanner(self):
        async def go():
            app = make_app()
            app.scanner_camera.show(isbn=MARTIAN)
            await app.bus.publish(ContextChanged("common", 1))
            await app.bus.publish(BookPlaced())
            await app.scanner.wait()
            first = app.dialogue.state
            app.scanner_camera.clear()
            await app.bus.publish(UserUtterance("bye"))
            await app.bus.publish(BookPlaced())
            await app.scanner.wait()
            return first, app.dialogue.state
        self.assertEqual(asyncio.run(go()), ("offering", "idle"))


class AppTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_style_changes_and_fixed_mode(self):
        app = make_app()
        seen = []
        app.bus.subscribe(StyleChanged, lambda e: seen.append(e.style.name))
        run(app, ContextChanged("quiet", 1), ContextChanged("quiet", 1, noise_db=65),
            ContextChanged("common", 3))
        self.assertEqual(seen, ["quiet_alone", "common_alone", "common_group"])
        asyncio.run(app.set_fixed(True))
        run(app, ContextChanged("quiet", 1))
        self.assertEqual(seen[-1], "fixed")
        self.assertEqual(app.style.name, "fixed")

    def test_reply_expression_blends_user_emotion(self):
        app = make_app()
        shown = []
        app.bus.subscribe(ExpressionRequested, lambda e: shown.append(e.emotion))
        run(app, EmotionDetected("sad", 0.9), ReplySentence("Sorry to hear that."))
        self.assertEqual(shown[-1], "sad")
        run(app, ReplySentence("Great news!", "happy"))
        self.assertEqual(shown[-1], "happy")
        run(app, EmotionDetected("happy", 0.2), ReplySentence("Okay."))    # below the threshold
        self.assertEqual(shown[-1], "neutral")

    def test_session_log_lines(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(Path(d))
            run(app, ContextChanged("quiet", 1), BookPlaced())
            lines = [json.loads(l) for l in (Path(d) / "session-test.jsonl").read_text().splitlines()]
        events = [l["event"] for l in lines]
        self.assertEqual(events[0], "ContextChanged")
        self.assertIn("StyleChanged", events)
        self.assertIn("BookPlaced", events)
        last = lines[-1]
        self.assertEqual(set(last), {"t", "event", "data", "context", "style", "fixed"})
        self.assertEqual(last["context"]["zone"], "quiet")
        self.assertEqual(last["style"]["name"], "quiet_alone")


if __name__ == "__main__":
    unittest.main()
