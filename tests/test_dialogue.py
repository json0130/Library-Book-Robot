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
from robot.services.book_llm import BookAssistant
from robot.services.llm import LLMError
from robot.core.dialogue import SCAN_FAILED_MESSAGE, choice_from
from robot.events import (BookDropped, BookIdentified, BookPlaced, ContextChanged, EmotionDetected,
                          ExpressionRequested, NavigationRequested, PersonEngaged, PersonLeft, ReplySentence,
                          ScanFailed, StyleChanged, UserUtterance)
from robot.services.book_graph import BookGraph, Shelf
from robot.services.books import Book, BookFinder, Catalog
from robot.services.topics import TopicVocabulary
from robot.config import ROOT

HITCH, MARTIAN, CLEAN_CODE = "9780345391803", "9780553418026", "9780132350884"


class _nothing:
    """A context manager that does nothing, for `with a if cond else _nothing():`."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class StubClient:
    """Stands in for the LLM client: answers from a list (or raises) and records every prompt."""

    def __init__(self, *replies, error=None):
        self.replies, self.error, self.prompts, self.history_seen = list(replies), error, [], []
        self.systems = []

    def chat(self, messages):
        self.systems.append(messages[0]["content"])
        self.prompts.append(messages[-1]["content"])
        self.history_seen.append(messages[1:-1])                # between the system prompt and this request
        if self.error:
            raise self.error
        return self.replies.pop(0)


def make_app(log_dir=None, llm=None, finder=None):
    cfg = load_config()
    cfg.robot.setdefault("books", {})["online_fallback"] = False
    cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0      # tests never touch the network
    assistant = BookAssistant(llm) if llm is not None else None
    app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False,
                    assistant=assistant, finder=finder, persist_storage=False)
    if log_dir is not None:
        app.session_log = SessionLog(log_dir, "test")
    app.now = [1000.0]                                   # the state machine's clock: tests move it with advance()
    app.dialogue.clock = lambda: app.now[0]
    return app


def run(app, *events):
    async def go():
        for e in events:
            await app.bus.publish(e)
    asyncio.run(go())


def advance(app, seconds, step=0.5):
    """Let `seconds` of the state machine's time pass, firing the timers that fall due."""
    async def go():
        for _ in range(int(seconds / step + 0.999)):
            app.now[0] += step
            await app.dialogue.tick()
    asyncio.run(go())


class DialogueTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)          # actuator logs are noise here
        self.addCleanup(logging.disable, logging.NOTSET)

    def path(self, app):
        """The states the machine entered, in order (internal transitions leave out)."""
        return [t[2] for t in app.dialogue.transitions if t[0] != t[2]]

    def test_full_book_flow(self):
        app = make_app()
        run(app, ContextChanged("quiet", 1), PersonEngaged(), BookPlaced(), scanned(HITCH),
            UserUtterance("I want to return it"), BookDropped(), UserUtterance("no thanks, bye"))
        self.assertEqual(self.path(app), ["IDLE", "ENGAGED.GREET", "ENGAGED.OFFER", "ENGAGED.SCAN", "ENGAGED.OFFER",
                                          "ENGAGED.STORE", "ENGAGED.ANYTHING_ELSE", "ENGAGED.FAREWELL"])
        self.assertEqual([t[1] for t in app.dialogue.transitions if t[0] != t[2]][:3],
                         ["start", "PersonEngaged", "greeted"])
        self.assertEqual(app.dialogue.storage.slot_of(HITCH), app.finder.find(HITCH).slot)

    def test_a_context_change_alone_starts_nothing_but_engaging_does(self):
        for zone, size in (("quiet", 1), ("common", 2)):
            app = make_app()
            run(app, ContextChanged(zone, size))
            self.assertEqual(app.dialogue.state, "IDLE")
            run(app, PersonEngaged())
            self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_visitor_can_start_by_speaking_and_leaving_ends_the_visit(self):
        app = make_app()
        run(app, ContextChanged("quiet", 1), UserUtterance("hello"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        run(app, BookPlaced())
        self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")
        run(app, PersonLeft(source="operator"))
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
        self.assertIsNone(app.dialogue.book)
        advance(app, 5)
        self.assertEqual(app.dialogue.state, "IDLE")

    def greetings(self, app):
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        return said

    def test_hello_is_said_once_per_visit_even_after_a_failed_scan(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1), PersonEngaged(), BookPlaced(), ScanFailed("x"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        run(app, ContextChanged("common", 2), ContextChanged("common", 1), PersonEngaged())     # the head count wobbles
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)

    def test_every_visit_starts_with_an_introduction_even_for_the_same_person(self):
        app = make_app()
        said = self.greetings(app)
        run(app, PersonEngaged((1,)))
        self.assertEqual(sum(t.startswith("Hello! My name is Stuart") for t in said), 1)
        run(app, PersonEngaged((1,)))                                              # still the same visit: not again
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)
        run(app, PersonLeft())
        advance(app, 5)
        self.assertEqual(app.dialogue.state, "IDLE")
        run(app, PersonEngaged((1,)))                                              # person 1 comes back: introduces itself
        self.assertEqual(sum(t.startswith("Hello! My name is Stuart") for t in said), 2)
        run(app, PersonLeft())
        advance(app, 5)
        app.now[0] += 3600
        run(app, PersonEngaged((2,)))                                              # and a new person too
        self.assertEqual(sum(t.startswith("Hello! My name is Stuart") for t in said), 3)
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_a_second_person_arriving_during_a_visit_does_not_interrupt_it(self):
        app = make_app()
        said = self.greetings(app)
        run(app, PersonEngaged((1,)), BookPlaced())                               # busy with person 1
        run(app, PersonEngaged((1, 2)))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)             # not interrupted
        self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")

    def test_scan_result_ignored_unless_scanning(self):
        app = make_app()
        run(app, BookIdentified("1", "Dune"))
        self.assertEqual(app.dialogue.state, "IDLE")

    def test_actions_follow_the_style(self):
        quiet, common = make_app(), make_app()
        run(quiet, ContextChanged("quiet", 1), PersonEngaged(), BookPlaced(), BookIdentified("1", "Dune"))
        run(common, ContextChanged("common", 1), PersonEngaged(), BookPlaced(), BookIdentified("1", "Dune"))
        quiet_cmds = [c for c, _ in quiet.actuators.lights.esp32.sent]
        self.assertIn("print", quiet_cmds)                  # quiet+alone: printed slip, no speech
        common_cmds = [c for c, _ in common.actuators.lights.esp32.sent]
        self.assertNotIn("print", common_cmds)              # common: spoken and on screen instead


def scanned(isbn):
    """A BookIdentified for a catalogue book, as the scanner would publish it."""
    app = make_app()
    book = app.finder.find(isbn)
    place = app.finder.locate(isbn)
    return BookIdentified(isbn=isbn, title=book.title, author=book.author, summary=book.summary,
                       shelf=book.shelf, slot=book.slot, found_in_catalog=True, call_number=book.call_number,
                       section=place.section, floor=place.floor, zone=place.zone,
                       map_x=place.map_x, map_y=place.map_y, map_yaw=place.map_yaw)


class BookChoicesTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def offer(self, zone="common", isbn=HITCH, event=None, llm=None):
        """An app that has just offered the three choices for a book."""
        app = make_app(llm=llm)
        app.said = []
        app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
        run(app, ContextChanged(zone, 1), PersonEngaged(), BookPlaced(), event or scanned(isbn))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
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
        sent = app.actuators.ring.esp32.sent
        self.assertIn("storage slot 4", app.said[-1])
        self.assertIn(("ring", {"slot": 4}), sent)                                  # the ring turns...
        self.assertIn(("slot_light", {"slot": 4, "on": True}), sent)                # ...the slot lights...
        self.assertIn("pad_vibrate", [c for c, _ in sent])                          # ...the pad pulses
        self.assertEqual(app.dialogue.state, "ENGAGED.STORE")                        # and it waits for the drop
        self.assertIsNone(app.dialogue.storage.slot_of(MARTIAN))                     # not stored until then
        run(app, BookDropped())
        said = " ".join(app.said)
        self.assertIn("Done: The Martian is in storage slot 4.", said)
        self.assertNotIn("It belongs", said)                                      # the shelf is not read out
        self.assertIn(("slot_light", {"slot": 4, "on": False}), sent)
        self.assertEqual(app.dialogue.storage.slot_of(MARTIAN), 4)
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_return_of_a_book_not_in_the_catalogue(self):
        unknown = BookIdentified(isbn="9780140449136", title="Some Online Book", found_in_catalog=False)
        app = self.offer(event=unknown)
        run(app, UserUtterance("return"))
        self.assertIn("isn't in our catalogue", app.said[-2])
        self.assertNotIn("ring", [c for c, _ in app.actuators.ring.esp32.sent])

    def test_explain_gives_the_catalogue_summary(self):
        app = self.offer()
        run(app, UserUtterance("explain"))
        self.assertIn("whisked off Earth", app.said[-2])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_explain_without_a_summary(self):
        app = self.offer(event=BookIdentified(isbn="9780140449136", title="Online Only"))
        run(app, UserUtterance("tell me about it"))
        self.assertIn("don't have a summary for Online Only", app.said[-2])

    def test_recommend_names_up_to_three_similar_books(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("recommend"))
        said = app.said[-2]
        self.assertIn("If you like The Martian", said)
        expected = [r.book.title for r in app.finder.recommend(MARTIAN, 3)]
        self.assertEqual(len(expected), 3)
        for title in expected:
            self.assertIn(title, said)
        self.assertNotIn("The Martian by", said)               # never recommends the book itself
        self.assertIn(f"I'd start with {expected[0]} because", said)       # ONE reason, for the top pick, without an LLM
        self.assertEqual(said.count("because"), 1)

    def test_recommend_edge_cases(self):
        app = self.offer(isbn=CLEAN_CODE)                       # shares no tags with anything
        run(app, UserUtterance("recommend"))
        self.assertIn("I don't have anything quite like Clean Code yet. What kind of books do you like?", app.said[-2])
        run(app, UserUtterance("space"))                                    # the answer is used as an interest
        self.assertIn("If you're interested in space", " ".join(app.said))
        app = self.offer(event=BookIdentified(isbn="9780140449136", title="Online Only"))
        run(app, UserUtterance("recommend"))
        self.assertIn("only recommend from books in our catalogue", app.said[-2])

    def test_unclear_answer_repeats_the_offer_and_stays(self):
        app = self.offer()
        run(app, UserUtterance("hmm, I don't know"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        self.assertIn("return it, hear about it", app.said[-1])
        run(app, UserUtterance("explain"))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_output_follows_the_style_channels(self):
        quiet = self.offer(zone="quiet", isbn=MARTIAN)
        run(quiet, UserUtterance("explain"))
        printed = [p["text"] for c, p in quiet.actuators.lights.esp32.sent if c == "print"]
        self.assertTrue(any("Mars" in t for t in printed))      # quiet+alone prints, never speaks
        common = self.offer(zone="common", isbn=MARTIAN)
        run(common, UserUtterance("explain"))
        self.assertNotIn("print", [c for c, _ in common.actuators.lights.esp32.sent])

    # ---- with the LLM ----
    def test_explain_uses_the_llm_with_the_catalogue_record(self):
        llm = StubClient("The Martian is a funny survival story on Mars.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("explain"))
        self.assertEqual(app.said[-2], "The Martian is a funny survival story on Mars.")
        self.assertIn("Summary: An astronaut is left behind", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def greetings(self, app):
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        return said

    # ---- where a book lives, and guiding the visitor there ----
    @staticmethod
    def fresh(**kw):
        """A new app that records what it says in app.said (like offer() does)."""
        app = make_app(**kw)
        app.said = []
        app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
        return app

    @staticmethod
    def navigation(app):
        events = []
        app.bus.subscribe(NavigationRequested, events.append)
        return events

    @staticmethod
    def finder_with(shelves):
        """A finder over the sample catalogue whose shelves.csv is replaced by `shelves` (a dict)."""
        catalog = Catalog.load(ROOT / "data" / "catalog.csv")
        graph = BookGraph(catalog, TopicVocabulary.load(ROOT / "data" / "topics.yaml"), shelves)
        return BookFinder(catalog, online_fallback=False, graph=graph)

    def test_return_without_location_data_still_gives_the_slot(self):
        app = self.offer(event=BookIdentified(isbn=MARTIAN, title="The Martian", slot=4, found_in_catalog=True))
        run(app, UserUtterance("return"), BookDropped())
        said = " ".join(app.said)
        self.assertIn("storage slot 4", said)
        self.assertNotIn("It belongs", said)
        self.assertIsNone(app.dialogue.guide_target)
        self.assertIn("Is there anything else", app.said[-1])

    def test_after_a_return_the_robot_does_not_offer_to_guide_to_the_book_they_handed_in(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        run(app, UserUtterance("return"), BookDropped())
        self.assertEqual(app.said[-1], "Is there anything else I can help with?")
        self.assertNotIn("guide", " ".join(app.said[-2:]))
        run(app, UserUtterance("yes please"))                                       # "yes" means more help, not a guide
        self.assertEqual(events, [])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_a_guide_to_a_shelf_still_works_when_they_ask_where_a_book_is(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        run(app, UserUtterance("where is the martian"))
        run(app, UserUtterance("yes please"))
        self.assertIn("Okay, follow me to The Martian. It's on shelf 2-SF-C, in the Science Fiction section on level 2.",
                      app.said[-1])
        (event,) = events
        self.assertEqual((event.isbn, event.title, event.shelf, event.section, event.floor, event.zone),
                         (MARTIAN, "The Martian", "2-SF-C", "Science Fiction", 2, "common"))
        self.assertEqual((event.x, event.y, event.yaw), (1.0, 6.0, 1.57))
        self.assertIn(("navigate", {"x": 1.0, "y": 6.0, "yaw": 1.57}), app.actuators.nav.esp32.sent)   # the fake base logs it

    def test_after_a_recommendation_the_offer_is_for_the_top_pick(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        top = app.finder.recommend(MARTIAN, 3)[0]
        run(app, UserUtterance("recommend"))
        self.assertIn(f"guide you to {top.book.title}", app.said[-1])
        run(app, UserUtterance("guide me"))
        self.assertEqual([e.title for e in events], [top.book.title])
        place = app.finder.locate(top.book.isbn13)
        self.assertEqual((events[0].shelf, events[0].x, events[0].y), (place.shelf, place.map_x, place.map_y))

    def group_offer(self, zone, size, isbn=MARTIAN, llm=None):
        """An app that has just offered the choices for a book to a group of `size` people in `zone`."""
        app = make_app(llm=llm)
        app.said = []
        app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
        run(app, ContextChanged(zone, size), BookPlaced(), scanned(isbn))
        return app

    def recommendation(self, app):
        run(app, UserUtterance("recommend"))
        return app.said[-2]                                                         # the last line is the follow-up question

    def test_quiet_alone_names_only_the_top_pick_with_one_reason(self):
        app = self.group_offer("quiet", 1)
        top, second = app.finder.recommend(MARTIAN, 3)[:2]
        said = self.recommendation(app)
        self.assertEqual(app.dialogue.get_style().recommendations, 1)
        self.assertIn(top.book.title, said)
        self.assertNotIn(second.book.title, said)
        self.assertEqual(said, f"If you like The Martian, you might enjoy {top.book.title} by {top.book.author} {top.because}.")
        self.assertNotIn("I'd start with", said)

    def test_common_group_names_three_and_gives_one_reason_for_the_first(self):
        app = self.group_offer("common", 3)
        picks = app.finder.recommend(MARTIAN, 3)
        said = self.recommendation(app)
        for pick in picks:
            self.assertIn(pick.book.title, said)
        self.assertEqual(said.count("because"), 1)
        self.assertIn(f"I'd start with {picks[0].book.title} {picks[0].because}.", said)

    def test_the_spoken_fallback_never_lists_several_topics(self):
        for zone, size in (("quiet", 1), ("common", 1), ("common", 3)):
            for isbn in (MARTIAN, HITCH, "9780345539434"):
                app = self.group_offer(zone, size, isbn)
                said = self.recommendation(app)
                self.assertNotIn("both about", said)
                self.assertNotIn(" stands out", said)
                self.assertEqual(said.count("because"), 1, said)

    def test_the_llm_gets_one_candidate_when_quiet_and_three_when_common(self):
        for zone, size, expected in (("quiet", 1, 1), ("common", 1, 3), ("common", 3, 3)):
            llm = StubClient("Nothing useful.")
            app = self.group_offer(zone, size, llm=llm)
            with self.assertLogs("robot.book_llm", level="WARNING"):                # the reply names no candidate: discarded
                run(app, UserUtterance("recommend"))
            prompt = llm.prompts[0]
            self.assertEqual(prompt.count("Candidate "), expected, (zone, size))
            self.assertEqual(prompt.count("Why it fits:"), expected)

    def test_the_llm_still_gets_every_reason_while_speech_gets_one(self):
        llm = StubClient("Nothing useful.")
        app = self.group_offer("common", 1, isbn="9780345539434", llm=llm)             # Cosmos -> A Brief History of Time first
        top = app.finder.recommend("9780345539434", 3)[0]
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("recommend"))
        self.assertGreater(len(top.reasons), 1)
        self.assertIn("Why it fits: " + "; ".join(top.reasons), llm.prompts[0])
        self.assertEqual(app.said[-2].count("because"), 1)                              # but the spoken fallback has one

    def test_the_full_reasons_are_logged(self):
        logging.disable(logging.NOTSET)                                                 # this class mutes INFO; here we want it
        self.addCleanup(logging.disable, logging.INFO)
        app = self.group_offer("common", 3, isbn="9780345539434")
        with self.assertLogs("robot.dialogue", level="INFO") as logs:
            run(app, UserUtterance("recommend"))
        text = "\n".join(logs.output)
        self.assertEqual(text.count("candidate "), 3)
        self.assertIn("candidate A Brief History of Time", text)
        self.assertIn("librarian's pick", text)                                         # the full list, not the one spoken reason
        self.assertIn("both about", text)

    def test_the_offer_follows_whichever_candidate_the_llm_named(self):
        picks = make_app().finder.recommend(MARTIAN, 3)
        llm = StubClient(f"How about {picks[1].book.title}? It is a good one.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        events = self.navigation(app)
        run(app, UserUtterance("recommend"), UserUtterance("take me there"))
        self.assertEqual([e.title for e in events], [picks[1].book.title])

    def test_explain_does_not_offer_a_guide_and_yes_means_another_book(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        run(app, UserUtterance("explain"))
        self.assertEqual(app.said[-1], "Is there anything else I can help with?")
        run(app, UserUtterance("yes"))
        self.assertEqual(events, [])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")                      # "yes" means: more help

    def test_an_unanswered_offer_expires_after_one_utterance(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        run(app, UserUtterance("return"), BookDropped(), UserUtterance("who wrote it?"), UserUtterance("yes"))
        self.assertEqual(events, [])                                              # that yes is no longer about the offer

    def test_guide_me_with_nowhere_to_go_says_so(self):
        app = self.fresh()
        events = self.navigation(app)
        run(app, UserUtterance("can you guide me?"))
        self.assertEqual(events, [])
        self.assertIn("Ask me where a book is", app.said[-1])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_guide_me_after_scanning_goes_to_the_scanned_book(self):
        app = self.offer(isbn=MARTIAN)
        events = self.navigation(app)
        run(app, UserUtterance("guide me"))
        self.assertEqual([e.title for e in events], ["The Martian"])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")                     # still waiting for a choice

    def test_where_is_a_title_says_where_and_offers_to_guide(self):
        for phrase in ("where is dune?", "Where's Dune", "where can I find DUNE", "find me dune please",
                       "where is 9780441172719", "show me where dune is"):
            app = self.fresh()
            events = self.navigation(app)
            run(app, UserUtterance(phrase))
            self.assertEqual(app.said[-1], "Dune is on shelf 2-SF-B, in the Science Fiction section on level 2. "
                                           "Would you like me to guide you there?", phrase)
            self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE", phrase)  # asking starts a visit
            self.assertEqual(events, [], phrase)                                    # it only told them
            run(app, UserUtterance("yes"))
            self.assertEqual([e.title for e in events], ["Dune"], phrase)

    def test_where_is_matches_titles_loosely(self):
        app = self.fresh()
        run(app, UserUtterance("where is the hitchhikers guide to the galaxy"))
        self.assertIn("The Hitchhiker's Guide to the Galaxy is on shelf 2-SF-A", app.said[-1])
        run(app, UserUtterance("where is frankenstien?"))
        self.assertIn("Frankenstein is on shelf 2-SF-C", app.said[-1])

    def test_where_does_this_book_go_means_the_scanned_book(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("where does this book go?"))
        self.assertIn("The Martian is on shelf 2-SF-C", app.said[-1])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_guide_me_to_a_title_asks_and_goes_in_one_step(self):
        app = self.fresh()
        events = self.navigation(app)
        run(app, UserUtterance("guide me to the martian"))
        self.assertEqual([e.title for e in events], ["The Martian"])
        self.assertIn("Okay, follow me to The Martian", app.said[-1])

    def test_questions_that_name_no_book_are_left_alone(self):
        for phrase in ("where is the bathroom?", "where do you live?"):
            app = self.fresh()
            events = self.navigation(app)
            run(app, UserUtterance(phrase))
            self.assertEqual(events, [], phrase)
            self.assertNotIn("is on shelf", app.said[-1], phrase)
            self.assertIn("Place a book on the pad", app.said[-1], phrase)         # the ordinary no-book answer

    def test_a_spoken_interest_with_no_book_gets_a_recommendation_not_a_nudge(self):
        app = self.fresh()
        events = self.navigation(app)
        run(app, UserUtterance("find me a book about space"))
        said = " ".join(app.said)
        self.assertIn("If you're interested in space, you might enjoy", said)
        self.assertEqual(events, [])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertIsNotNone(app.dialogue.guide_target)                            # and it can guide to the top pick

    def test_where_works_in_a_quiet_zone_too_through_the_style_channels(self):
        app = self.fresh()
        run(app, ContextChanged("quiet", 1), UserUtterance("where is cosmos"))
        self.assertIn("Cosmos is on shelf 3-SCI-A, in the Science section on level 3", app.said[-1])

    def test_a_shelf_with_no_map_pose_is_described_but_not_driven_to(self):
        app = self.fresh(finder=self.finder_with({"2-SF-B": Shelf("2-SF-B", "Science Fiction", 2, "common")}))
        events = self.navigation(app)
        run(app, UserUtterance("where is dune"))
        self.assertEqual(app.said[-1], "Dune is on shelf 2-SF-B, in the Science Fiction section on level 2.")   # no offer
        run(app, UserUtterance("guide me"))
        self.assertIn("I don't have a map position for Dune yet", app.said[-1])
        self.assertIn("It's on shelf 2-SF-B", app.said[-1])
        self.assertEqual(events, [])

    def test_a_shelf_missing_from_shelves_csv_still_gives_section_and_floor(self):
        app = self.fresh(finder=self.finder_with({}))
        run(app, UserUtterance("where is dune"))
        self.assertEqual(app.said[-1], "Dune is on shelf 2-SF-B, in the Science Fiction section on level 2.")

    def test_a_book_with_no_known_location_sends_the_visitor_to_the_help_desk(self):
        finder = BookFinder(Catalog([Book(MARTIAN, "The Martian")]), online_fallback=False)
        app = self.fresh(finder=finder)
        run(app, UserUtterance("where is the martian"))
        self.assertEqual(app.said[-1], "I don't know where The Martian is shelved yet. The help desk can tell you.")

    def test_goodbye_still_wins_over_the_offer(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("return"), BookDropped(), UserUtterance("no thanks"))
        self.assertIn("Enjoy your reading", app.said[-1])

    def test_a_new_scan_or_everyone_leaving_forgets_the_target(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("where is dune"))
        self.assertIsNotNone(app.dialogue.guide_target)
        run(app, BookPlaced(), scanned(HITCH))
        self.assertIsNone(app.dialogue.guide_target)
        run(app, UserUtterance("where is dune"))
        self.assertIsNotNone(app.dialogue.guide_target)
        run(app, PersonLeft(source="operator"))
        self.assertIsNone(app.dialogue.guide_target)
        self.assertFalse(app.dialogue.awaiting_guide)

    # ---- before any book: who is the robot, small talk ----
    def test_the_robot_is_stuart(self):
        app = make_app()
        said = self.greetings(app)
        run(app, PersonEngaged())
        self.assertIn("My name is Stuart", said[0])

    def test_a_bare_hi_gets_a_greeting_without_the_llm(self):
        llm = StubClient("should not be asked")
        app = make_app(llm=llm)
        said = self.greetings(app)
        run(app, UserUtterance("hi"))
        self.assertEqual(llm.prompts, [])
        self.assertIn("My name is Stuart", said[0])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_a_second_hi_after_the_hello_does_not_introduce_stuart_again(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1), PersonEngaged(), UserUtterance("hi"))
        self.assertIn("My name is Stuart", said[0])
        self.assertNotIn("Stuart", said[-1])
        self.assertTrue(said[-1].startswith("Hi!"))

    def test_would_i_like_it_is_a_question_about_the_book_not_a_recommendation(self):
        llm = StubClient("If you enjoy funny stories, yes: it has plenty of humour.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("would I like it if I enjoy funny stories?"))
        self.assertEqual(app.said[-2], "If you enjoy funny stories, yes: it has plenty of humour.")
        self.assertIn("Title: The Martian", llm.prompts[0])
        self.assertNotIn("Candidate", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_small_talk_before_a_book_goes_to_the_llm_without_any_catalogue(self):
        llm = StubClient("I'm Stuart. Put a book on my pad and I can help.", "I can return books, explain them and recommend more.")
        app = make_app(llm=llm)
        said = self.greetings(app)
        run(app, UserUtterance("hi what is your name?"), UserUtterance("what can you do?"))
        self.assertEqual(said[-2:], ["I'm Stuart. Put a book on my pad and I can help.",
                                     "I can return books, explain them and recommend more."])
        self.assertNotIn("Catalogue entry", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        self.assertIn("do not name or describe specific books", llm.systems[0])
        self.assertIn("hi what is your name?", str(llm.history_seen[1]))       # the 2nd turn remembers the 1st

    def test_who_are_you_works_when_the_llm_is_down_or_missing(self):
        for llm in (None, StubClient(error=LLMError("down"))):
            app = make_app(llm=llm)
            said = self.greetings(app)
            with self.assertLogs("robot.book_llm", level="WARNING") if llm else _nothing():
                run(app, UserUtterance("hi, what is your name?"))
            self.assertIn("I'm Stuart", said[-1])

    def test_other_questions_without_an_llm_nudge_towards_the_pad(self):
        app = make_app()
        said = self.greetings(app)
        run(app, UserUtterance("what is the weather like?"))
        self.assertIn("Place a book on the pad", said[-1])

    def test_who_are_you_after_a_book_is_answered_about_the_robot(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("can you introduce yourself?"))
        self.assertIn("I'm Stuart", app.said[-2])

    # ---- follow-up questions about the scanned book ----
    def test_related_books_after_an_explanation_is_a_recommendation_not_another_book(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("explain"), UserUtterance("do you know any related books to this?"))
        self.assertIn("If you like The Martian, you might enjoy", " ".join(app.said))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_choices_can_be_asked_again_after_the_first_answer(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("explain"), UserUtterance("what is it about again?"))
        self.assertEqual(sum("astronaut" in t for t in app.said), 2)

    def test_the_chat_before_the_first_book_carries_on_after_the_scan(self):
        llm = StubClient("I'm doing great, thanks!", "It is about Mars.")
        app = make_app(llm=llm)
        run(app, ContextChanged("common", 1), PersonEngaged(), UserUtterance("how are you?"), BookPlaced(), scanned(MARTIAN),
            UserUtterance("explain"))
        asked = llm.history_seen[1]
        self.assertEqual([m["content"] for m in asked if m["role"] == "user"], ["how are you?"])
        self.assertTrue(any("doing great" in m["content"] for m in asked))

    def test_a_free_question_goes_to_the_llm_with_the_catalogue_record(self):
        llm = StubClient("It was written by Andy Weir.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("who wrote it?"))
        self.assertEqual(app.said[-2], "It was written by Andy Weir.")
        self.assertIn("Title: The Martian", llm.prompts[0])
        self.assertIn("who wrote it?", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")      # and it asks if there is more

    def test_a_follow_up_question_sees_what_was_said_before(self):
        llm = StubClient("It is about an astronaut stranded on Mars.", "Yes, he grows potatoes to survive.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("explain"), UserUtterance("does he get food somehow?"))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertEqual(app.said[-2], "Yes, he grows potatoes to survive.")
        seen = llm.history_seen[1]                                         # what the 2nd request carried
        self.assertEqual([m["role"] for m in seen],
                         ["assistant", "assistant", "assistant", "user", "assistant", "assistant"])
        self.assertIn("Hello! My name is Stuart", seen[0]["content"])             # even the canned lines are remembered
        self.assertIn("You have The Martian", seen[2]["content"])
        self.assertEqual(seen[3]["content"], "explain")                    # the visitor's real words
        self.assertIn("astronaut stranded on Mars", seen[4]["content"])

    def test_llm_down_for_a_free_question_says_so_and_keeps_the_book(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("will I enjoy it?"))
        self.assertIn("Sorry, I'm not sure how to answer that", app.said[-2])
        run(app, UserUtterance("explain"), UserUtterance("is it long?"))
        self.assertIn("Sorry, I'm not sure how to answer that", app.said[-2])
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertEqual(app.dialogue.book.title, "The Martian")

    def test_who_wrote_it_is_answered_from_the_catalogue_without_the_llm(self):
        for app in (self.offer(isbn=MARTIAN), self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))):
            with self.assertLogs("robot.book_llm", level="WARNING") if app.dialogue.assistant else _nothing():
                run(app, UserUtterance("who wrote it?"))
            self.assertEqual(app.said[-2], "The Martian was written by Andy Weir.")
            self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_im_done_in_any_wording_ends_the_conversation_even_with_typos(self):
        for phrase in ("o thats everyhing i think", "no i dont have any quetsions", "no", "Nope, thanks!",
                       "nothing else", "I'm good", "that's everything"):
            llm = StubClient("It is about Mars.")
            app = self.offer(isbn=MARTIAN, llm=llm)
            run(app, UserUtterance("explain"))
            asked = len(llm.prompts)
            run(app, UserUtterance(phrase))
            self.assertEqual(len(llm.prompts), asked, phrase)               # not sent to the LLM
            self.assertIn("Enjoy your reading", app.said[-1], phrase)
            self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")

    def test_a_question_starting_with_no_or_containing_thing_words_is_not_done(self):
        for phrase in ("no wait, who wrote it?", "is there anything else by him?", "what about the ending?"):
            app = self.offer(isbn=MARTIAN)
            run(app, UserUtterance("explain"), UserUtterance(phrase))
            self.assertNotIn("Enjoy your reading", app.said[-1], phrase)

    def test_an_unclear_remark_after_an_answer_is_acknowledged_not_refused(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))
        run(app, UserUtterance("explain"))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("hmm interesting"))
        self.assertIn("place it on my pad", app.said[-2])
        self.assertNotIn("Sorry", app.said[-2])

    def test_a_book_outside_the_catalogue_never_gets_a_free_question_to_the_llm(self):
        llm = StubClient("made up")
        app = self.offer(llm=llm, event=BookIdentified("9781234567897"))
        run(app, UserUtterance("who wrote it?"))
        self.assertEqual(llm.prompts, [])

    def test_another_book_after_an_answer_asks_for_the_next_book(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("explain"), UserUtterance("can I scan another book?"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        self.assertIn("another book", app.said[-1])
        self.assertIsNone(app.dialogue.book)

    def test_yes_after_an_answer_means_more_help_and_offers_the_choices_again(self):
        for phrase in ("yes please", "Yeah."):
            app = self.offer(isbn=MARTIAN)
            run(app, UserUtterance("explain"), UserUtterance(phrase))
            self.assertEqual(app.dialogue.state, "ENGAGED.OFFER", phrase)
            self.assertIn("return it, hear about it", app.said[-1])

    def test_recommend_uses_the_llm_with_only_catalogue_candidates(self):
        picks = make_app().finder.recommend(MARTIAN, 3)
        expected = [r.book.title for r in picks]
        llm = StubClient(f"You might enjoy {expected[0]}.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("recommend"))
        self.assertEqual(app.said[-2], f"You might enjoy {expected[0]}.")
        for pick in picks:
            self.assertIn(f"Title: {pick.book.title}", llm.prompts[0])
            self.assertIn("Why it fits: " + "; ".join(pick.reasons), llm.prompts[0])      # the reasons reach the LLM

    def test_recommend_that_names_no_candidate_falls_back_to_the_catalogue_text(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient("Read War and Peace."))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("recommend"))
        self.assertIn("If you like The Martian, you might enjoy", app.said[-2])

    def test_llm_down_falls_back_to_the_catalogue_text(self):
        llm = StubClient(error=LLMError("server unreachable"))
        app = self.offer(isbn=MARTIAN, llm=llm)
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("explain"))
        self.assertIn("An astronaut is left behind on Mars", app.said[-2])        # the catalogue summary
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_return_never_asks_the_llm(self):
        llm = StubClient()
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("return"), BookDropped())
        self.assertEqual(llm.prompts, [])
        self.assertIn("storage slot 4", " ".join(app.said))

    def test_books_outside_the_catalogue_never_go_to_the_llm(self):
        llm = StubClient()
        unknown = BookIdentified(isbn="9780140449136", title="Online Only", author="Someone", found_in_catalog=False)
        app = self.offer(event=unknown, llm=llm)
        run(app, UserUtterance("explain"))
        self.assertIn("don't have a summary for Online Only", app.said[-2])
        app = self.offer(event=unknown, llm=llm)
        run(app, UserUtterance("recommend"))
        self.assertEqual(llm.prompts, [])

    def test_a_new_book_starts_a_fresh_llm_conversation(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient("It is about Mars.", "More Mars."))
        run(app, UserUtterance("explain"))
        self.assertEqual(len(app.dialogue.assistant.history), 6)           # hello, offers, "explain", answer, "anything else?"
        run(app, BookPlaced(), scanned(HITCH))
        history = app.dialogue.assistant.history                           # only the new book's announcement is left
        self.assertEqual([m["role"] for m in history], ["assistant"])
        self.assertIn("Hitchhiker", history[0]["content"])
        run(app, UserUtterance("everyone"), PersonLeft(source="operator"))
        self.assertEqual(app.dialogue.assistant.history, [])

    def test_scan_failed_returns_to_idle_and_asks_for_a_flat_book(self):
        app = make_app()
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        run(app, ContextChanged("quiet", 1), BookPlaced())
        self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")
        run(app, ScanFailed("no ISBN barcode found within 5 s"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")                 # try again
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
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")

    def test_a_second_failed_scan_goes_back_to_the_offer(self):
        app = make_app()
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        run(app, ContextChanged("common", 1), BookPlaced(), ScanFailed("x"), BookPlaced(), ScanFailed("y"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        self.assertEqual(said[0], SCAN_FAILED_MESSAGE)
        self.assertIn("still can't read that barcode", said[-1])
        run(app, BookPlaced(), scanned(MARTIAN))                           # a good read clears the count
        self.assertEqual(app.dialogue.session.scan_failures, 0)

    def test_a_book_shown_while_the_robot_waits_for_a_choice_is_scanned_instead(self):
        app = self.offer()
        run(app, BookPlaced())
        self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")
        run(app, BookIdentified(MARTIAN, "The Martian"))
        self.assertEqual(app.dialogue.book.title, "The Martian")
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_scan_failed_is_ignored_unless_scanning(self):
        app = self.offer()
        run(app, ScanFailed("late"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_bus_flow_from_placement_through_the_scanner(self):
        async def go():
            app = make_app()
            app.scanner_camera.show(isbn=MARTIAN)
            await app.bus.publish(ContextChanged("common", 1))
            await app.bus.publish(PersonEngaged())
            await app.bus.publish(BookPlaced())
            await app.scanner.wait()
            first = app.dialogue.state
            app.scanner_camera.clear()
            await app.bus.publish(UserUtterance("bye"))
            await app.bus.publish(BookPlaced())
            await app.scanner.wait()
            return first, app.dialogue.state
        # the second book can't be read, so the visitor who just said bye is asked to try again
        self.assertEqual(asyncio.run(go()), ("ENGAGED.OFFER", "ENGAGED.AWAIT_BOOK"))


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
