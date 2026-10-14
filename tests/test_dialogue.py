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
from robot.core.dialogue import OFFER, SCAN_FAILED_MESSAGE, choice_from
from robot.events import (BookPlaced, BookScanned, ContextChanged, EmotionDetected, ExpressionRequested,
                          ReplySentence, ScanFailed, StyleChanged, UserUtterance)

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


def make_app(log_dir=None, llm=None):
    cfg = load_config()
    cfg.robot.setdefault("books", {})["online_fallback"] = False      # tests never touch the network
    assistant = BookAssistant(llm) if llm is not None else None
    app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False,
                    assistant=assistant)
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

    def greetings(self, app):
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        return said

    def test_hello_is_said_once_per_visit_even_after_a_failed_scan(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1), BookPlaced(), ScanFailed("x"))
        self.assertEqual(app.dialogue.state, "idle")
        run(app, ContextChanged("common", 2), ContextChanged("common", 1))       # the head count wobbles
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)

    def test_each_person_is_greeted_once_even_after_leaving_and_coming_back(self):
        app = make_app()
        now = [1000.0]
        app.dialogue.clock = lambda: now[0]
        said = self.greetings(app)
        run(app, ContextChanged("common", 1, person_ids=(1,)))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)
        now[0] += 3600                                                            # an hour later: the cooldown is no excuse
        run(app, ContextChanged("common", 0), ContextChanged("common", 1, person_ids=(1,)))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)             # person 1 again: no hello
        self.assertEqual(app.dialogue.state, "idle")
        run(app, ContextChanged("common", 0), ContextChanged("common", 1, person_ids=(2,)))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 2)             # person 2 is new: hello
        self.assertEqual(app.dialogue.state, "invited")

    def test_a_new_person_arriving_next_to_a_greeted_one_is_greeted_when_the_robot_is_free(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1, person_ids=(1,)), BookPlaced())      # busy with person 1
        run(app, ContextChanged("common", 2, person_ids=(1, 2)))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)             # not interrupted
        run(app, ScanFailed("x"), ContextChanged("common", 2, person_ids=(1, 2)))  # free again, person 2 still not greeted
        self.assertEqual(sum(t.startswith("Hello") for t in said), 2)

    def test_coming_back_within_the_cooldown_is_not_greeted_again(self):
        app = make_app()
        now = [1000.0]
        app.dialogue.clock = lambda: now[0]
        said = self.greetings(app)
        run(app, ContextChanged("common", 1), ContextChanged("common", 0))
        now[0] += 60                                                              # 60 s < the 120 s cooldown
        run(app, ContextChanged("common", 1))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 1)
        self.assertEqual(app.dialogue.state, "idle")
        now[0] += 120                                                             # a new visit after the cooldown
        run(app, ContextChanged("common", 0), ContextChanged("common", 1))
        self.assertEqual(sum(t.startswith("Hello") for t in said), 2)
        self.assertEqual(app.dialogue.state, "invited")

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

    def offer(self, zone="common", isbn=HITCH, event=None, llm=None):
        """An app that has just offered the three choices for a book."""
        app = make_app(llm=llm)
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

    # ---- with the LLM ----
    def test_explain_uses_the_llm_with_the_catalogue_record(self):
        llm = StubClient("The Martian is a funny survival story on Mars.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("explain"))
        self.assertEqual(app.said[-2], "The Martian is a funny survival story on Mars.")
        self.assertIn("Summary: An astronaut is left behind", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "closing")

    def greetings(self, app):
        said = []
        app.bus.subscribe(ReplySentence, lambda e: said.append(e.text))
        return said

    # ---- before any book: who is the robot, small talk ----
    def test_the_robot_is_stuart(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1))
        self.assertIn("I'm Stuart", said[0])

    def test_a_bare_hi_gets_a_greeting_without_the_llm(self):
        llm = StubClient("should not be asked")
        app = make_app(llm=llm)
        said = self.greetings(app)
        run(app, UserUtterance("hi"))
        self.assertEqual(llm.prompts, [])
        self.assertIn("I'm Stuart", said[0])
        self.assertEqual(app.dialogue.state, "invited")

    def test_a_second_hi_after_the_hello_does_not_introduce_stuart_again(self):
        app = make_app()
        said = self.greetings(app)
        run(app, ContextChanged("common", 1), UserUtterance("hi"))
        self.assertIn("I'm Stuart", said[0])
        self.assertNotIn("Stuart", said[1])
        self.assertTrue(said[1].startswith("Hi!"))

    def test_would_i_like_it_is_a_question_about_the_book_not_a_recommendation(self):
        llm = StubClient("If you enjoy funny stories, yes: it has plenty of humour.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("would I like it if I enjoy funny stories?"))
        self.assertEqual(app.said[-1], "If you enjoy funny stories, yes: it has plenty of humour.")
        self.assertIn("Title: The Martian", llm.prompts[0])
        self.assertNotIn("Candidate", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "offering")

    def test_small_talk_before_a_book_goes_to_the_llm_without_any_catalogue(self):
        llm = StubClient("I'm Stuart. Put a book on my pad and I can help.", "I can return books, explain them and recommend more.")
        app = make_app(llm=llm)
        said = self.greetings(app)
        run(app, UserUtterance("hi what is your name?"), UserUtterance("what can you do?"))
        self.assertEqual(said, ["I'm Stuart. Put a book on my pad and I can help.",
                                "I can return books, explain them and recommend more."])
        self.assertNotIn("Catalogue entry", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "invited")
        self.assertIn("do not name or describe specific books", llm.systems[0])
        self.assertIn("hi what is your name?", str(llm.history_seen[1]))       # the 2nd turn remembers the 1st

    def test_who_are_you_works_when_the_llm_is_down_or_missing(self):
        for llm in (None, StubClient(error=LLMError("down"))):
            app = make_app(llm=llm)
            said = self.greetings(app)
            with self.assertLogs("robot.book_llm", level="WARNING") if llm else _nothing():
                run(app, UserUtterance("hi, what is your name?"))
            self.assertIn("I'm Stuart", said[0])

    def test_other_questions_without_an_llm_nudge_towards_the_pad(self):
        app = make_app()
        said = self.greetings(app)
        run(app, UserUtterance("what is the weather like?"))
        self.assertIn("Place a book on the pad", said[0])

    def test_who_are_you_after_a_book_is_answered_about_the_robot(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("can you introduce yourself?"))
        self.assertIn("I'm Stuart", app.said[-1])

    # ---- follow-up questions about the scanned book ----
    def test_related_books_after_an_explanation_is_a_recommendation_not_another_book(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("explain"), UserUtterance("do you know any related books to this?"))
        self.assertIn("If you like The Martian, you might enjoy", " ".join(app.said))
        self.assertEqual(app.dialogue.state, "closing")

    def test_choices_can_be_asked_again_after_the_first_answer(self):
        app = self.offer(isbn=MARTIAN)
        run(app, UserUtterance("explain"), UserUtterance("what is it about again?"))
        self.assertEqual(sum("astronaut" in t for t in app.said), 2)

    def test_the_chat_before_the_first_book_carries_on_after_the_scan(self):
        llm = StubClient("I'm doing great, thanks!", "It is about Mars.")
        app = make_app(llm=llm)
        run(app, ContextChanged("common", 1), UserUtterance("how are you?"), BookPlaced(), scanned(MARTIAN),
            UserUtterance("explain"))
        asked = llm.history_seen[1]
        self.assertEqual([m["content"] for m in asked if m["role"] == "user"], ["how are you?"])
        self.assertTrue(any("doing great" in m["content"] for m in asked))

    def test_a_free_question_goes_to_the_llm_with_the_catalogue_record(self):
        llm = StubClient("It was written by Andy Weir.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("who wrote it?"))
        self.assertEqual(app.said[-1], "It was written by Andy Weir.")
        self.assertIn("Title: The Martian", llm.prompts[0])
        self.assertIn("who wrote it?", llm.prompts[0])
        self.assertEqual(app.dialogue.state, "offering")                   # still waiting for a choice

    def test_a_follow_up_question_sees_what_was_said_before(self):
        llm = StubClient("It is about an astronaut stranded on Mars.", "Yes, he grows potatoes to survive.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("explain"), UserUtterance("does he get food somehow?"))
        self.assertEqual(app.dialogue.state, "closing")
        self.assertEqual(app.said[-1], "Yes, he grows potatoes to survive.")
        seen = llm.history_seen[1]                                         # what the 2nd request carried
        self.assertEqual([m["role"] for m in seen], ["assistant", "assistant", "user", "assistant", "assistant"])
        self.assertIn("Hello! I'm Stuart", seen[0]["content"])             # even the canned lines are remembered
        self.assertIn("You have The Martian", seen[1]["content"])
        self.assertEqual(seen[2]["content"], "explain")                    # the visitor's real words
        self.assertIn("astronaut stranded on Mars", seen[3]["content"])

    def test_llm_down_for_a_free_question_says_so_and_keeps_the_book(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))
        with self.assertLogs("robot.book_llm", level="WARNING"):
            run(app, UserUtterance("will I enjoy it?"))
        self.assertEqual(app.said[-1], OFFER)
        run(app, UserUtterance("explain"), UserUtterance("is it long?"))
        self.assertIn("Sorry, I'm not sure how to answer that", app.said[-1])
        self.assertEqual(app.dialogue.state, "closing")
        self.assertEqual(app.dialogue.book.title, "The Martian")

    def test_who_wrote_it_is_answered_from_the_catalogue_without_the_llm(self):
        for app in (self.offer(isbn=MARTIAN), self.offer(isbn=MARTIAN, llm=StubClient(error=LLMError("down")))):
            with self.assertLogs("robot.book_llm", level="WARNING") if app.dialogue.assistant else _nothing():
                run(app, UserUtterance("who wrote it?"))
            self.assertEqual(app.said[-1], "The Martian was written by Andy Weir.")
            self.assertEqual(app.dialogue.state, "offering")

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
            self.assertEqual(app.dialogue.state, "closing")

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
        self.assertIn("place it on my pad", app.said[-1])
        self.assertNotIn("Sorry", app.said[-1])

    def test_a_book_outside_the_catalogue_never_gets_a_free_question_to_the_llm(self):
        llm = StubClient("made up")
        app = self.offer(llm=llm, event=BookScanned("9781234567897"))
        run(app, UserUtterance("who wrote it?"))
        self.assertEqual(llm.prompts, [])

    def test_another_book_or_yes_after_an_answer_asks_for_the_next_book(self):
        for phrase in ("can I scan another book?", "yes please", "Yeah."):
            app = self.offer(isbn=MARTIAN)
            run(app, UserUtterance("explain"), UserUtterance(phrase))
            self.assertEqual(app.dialogue.state, "invited", phrase)
            self.assertIn("another book", app.said[-1])

    def test_recommend_uses_the_llm_with_only_catalogue_candidates(self):
        expected = [b.title for b in make_app().finder.similar_to(MARTIAN, 3)]
        llm = StubClient(f"You might enjoy {expected[0]}.")
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("recommend"))
        self.assertEqual(app.said[-2], f"You might enjoy {expected[0]}.")
        for title in expected:
            self.assertIn(f"Title: {title}", llm.prompts[0])

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
        self.assertEqual(app.dialogue.state, "closing")

    def test_return_never_asks_the_llm(self):
        llm = StubClient()
        app = self.offer(isbn=MARTIAN, llm=llm)
        run(app, UserUtterance("return"))
        self.assertEqual(llm.prompts, [])
        self.assertIn("storage slot 4", " ".join(app.said))

    def test_books_outside_the_catalogue_never_go_to_the_llm(self):
        llm = StubClient()
        unknown = BookScanned(isbn="9780140449136", title="Online Only", author="Someone", found_in_catalog=False)
        app = self.offer(event=unknown, llm=llm)
        run(app, UserUtterance("explain"))
        self.assertIn("don't have a summary for Online Only", app.said[-2])
        app = self.offer(event=unknown, llm=llm)
        run(app, UserUtterance("recommend"))
        self.assertEqual(llm.prompts, [])

    def test_a_new_book_starts_a_fresh_llm_conversation(self):
        app = self.offer(isbn=MARTIAN, llm=StubClient("It is about Mars.", "More Mars."))
        run(app, UserUtterance("explain"))
        self.assertEqual(len(app.dialogue.assistant.history), 5)           # announcement, "explain", answer, "anything else?"...
        run(app, BookPlaced(), scanned(HITCH))
        history = app.dialogue.assistant.history                           # only the new book's announcement is left
        self.assertEqual([m["role"] for m in history], ["assistant"])
        self.assertIn("Hitchhiker", history[0]["content"])
        run(app, UserUtterance("everyone"), ContextChanged("common", 0))
        self.assertEqual(app.dialogue.assistant.history, [])

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

    def test_a_book_shown_while_the_robot_waits_for_a_choice_is_scanned_instead(self):
        app = self.offer()
        run(app, BookPlaced())
        self.assertEqual(app.dialogue.state, "scanning")
        run(app, BookScanned(MARTIAN, "The Martian"))
        self.assertEqual(app.dialogue.book.title, "The Martian")
        self.assertEqual(app.dialogue.state, "offering")

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
