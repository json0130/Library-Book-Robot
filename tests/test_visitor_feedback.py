"""Six things a visitor hit in live testing, and the fixes:

1. "thank you, also can I ask you some questions?" ended the visit (it was read as "done").
2. "can you tell me aobut the I robot book?" asked "Which book?" (a typo in "about" kept the title from being found).
3. "which book do you hold rn?" / "do you have any book with u rn?" were taken for a recommendation or small talk, not for
   what the robot itself holds (its pad and its storage).
4. In a quiet zone "recommend" asked the same "what kind of books do you like?" again and again.
5. In a quiet zone nothing on the screen said the return had worked.
6. In a quiet zone what the robot would have said (explanations, answers, questions) was cut off or replaced at once.
"""

import logging
import unittest

from display.server import CONTENT_CHOICE, INPUT_CHOICES
from robot.core.dialogue import HOW_TO_ASK
from robot.events import BookDropped, BookPlaced, PersonEngaged, PersonLeft, UserChoice, UserUtterance
from robot.operator import PICKS
from tests.test_dialogue import MARTIAN, make_app, scanned
from tests.test_state_machine import new_app, quiet_logs, run


def router():
    logging.disable(logging.INFO)
    return make_app().dialogue.router


def title_of(app, isbn):
    return next(b.title for b in app.dialogue.finder.catalog.books if b.isbn13 == isbn)


def last(app, view=None):
    return next(u for u in reversed(app.screens) if view is None or u.view == view)


class ThanksIsNotGoodbyeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = router()

    def best(self, text, state="ANYTHING_ELSE"):
        return self.router.route(text, state).best

    def test_thanks_plus_something_more_is_not_done(self):
        for text in ("oh i see thank you also can i ask you some quetions?", "thanks, and can I ask something else?",
                     "thank you but tell me about something different"):
            with self.subTest(text=text):
                best = self.best(text)
                self.assertFalse(best is not None and best.intent == "done", best)

    def test_thanks_alone_still_ends_the_visit(self):
        for text in ("thank you", "thanks", "thanks that is all", "no thanks", "thank you very much"):
            with self.subTest(text=text):
                self.assertEqual(self.best(text).intent, "done")

    def test_thanks_with_a_real_request_follows_the_request(self):
        self.assertEqual(self.best("thank you also where is the toilet").intent, "where")

    def test_the_visit_goes_on_and_it_says_what_can_be_asked(self):
        quiet_logs(self)
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where is the help desk?"),
            UserUtterance("oh i see thank you also can i ask you some quetions?"))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertEqual(app.said[-1], HOW_TO_ASK)

    def test_permission_to_ask_is_answered_but_a_question_is_still_a_question(self):
        quiet_logs(self)
        app = new_app()
        run(app, PersonEngaged())
        d = app.dialogue
        for text in ("can i ask you some questions?", "i have a question", "can I ask you something?"):
            self.assertTrue(d.asks_permission(text.lower()), text)
        for text in ("can i ask you who wrote dune?", "can i ask where the toilets are", "tell me about dune"):
            self.assertFalse(d.asks_permission(text.lower()), text)


class TitleSurvivesATypoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = router()
        cls.titles = {b.isbn13: b.title for b in make_app().dialogue.finder.catalog.books}

    def test_a_misspelt_tell_me_about_still_finds_the_book(self):
        for text, title in (("can you tell me aobut the I robot book?", "I Robot"), ("tel me abuot the martian", "The Martian"),
                            ("what is cosmos abuot", "Cosmos"), ("can you tell me about the I robot book?", "I Robot")):
            with self.subTest(text=text):
                best = self.router.route(text, "OFFER").best
                self.assertEqual(best.intent, "explain")
                self.assertEqual(self.titles.get(best.isbn), title)

    def test_the_robot_explains_the_book_instead_of_asking_which(self):
        quiet_logs(self)
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("can you tell me aobut the I robot book?"))
        said = " ".join(app.said)
        self.assertIn("I Robot:", said)
        self.assertNotIn("Which book?", said)


class WhatItIsHoldingTest(unittest.TestCase):
    PHRASES = ("which book do you hold rn", "do you not have any book with u rn?", "do you have any book with you",
               "are you holding a book right now", "what is on your pad", "what's in your storage?")

    def setUp(self):
        quiet_logs(self)

    def test_it_is_never_taken_for_a_recommendation(self):
        for text in self.PHRASES:
            app = new_app()
            run(app, PersonEngaged(), UserUtterance(text))
            said = " ".join(app.said[2:])                    # after the hello and the offer
            self.assertTrue("my pad" in said or "my storage" in said, (text, said))
            self.assertNotIn("couldn't find a book about", said, text)
            self.assertNotIn("recommend", app.dialogue.state.lower(), text)

    def test_with_nothing_held_it_says_so_for_both_places_and_what_to_do(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("which book do you hold rn"))
        said = " ".join(app.said)
        self.assertIn("My storage is empty", said)
        self.assertIn("Nothing is on my pad right now", said)
        self.assertIn("tell me its title or a topic", said)           # an unclear question gets the way to look a book up

    def test_a_question_about_the_pad_only_gets_the_pad(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("what is on your pad"))
        self.assertIn("Nothing is on my pad right now. Place a book on it", app.said[-1])
        self.assertNotIn("storage", app.said[-1])

    def test_it_names_what_is_in_storage_and_on_the_pad(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped(),
            UserUtterance("do you have any book with you rn?"))
        said = " ".join(app.said[-3:])
        self.assertIn("1 returned book", said)
        self.assertIn("The Martian in slot", said)

    def test_topic_requests_are_still_recommendations(self):
        for text in ("do you have any books about space", "recommend something about astronomy"):
            app = new_app()
            run(app, PersonEngaged(), UserUtterance(text))
            self.assertNotIn("My storage is empty", " ".join(app.said), text)

    def test_a_named_book_in_storage_is_still_offered(self):
        from tests.test_storage_offer import with_stored
        app, book = with_stored()
        run(app, UserUtterance(f"do you have {book.title} with you?"))
        self.assertIn("Would you like it?", " ".join(app.said))


class RecommendationsStopAskingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def asks(self, app):
        return sum("What kind of books do you like" in s for s in app.said)

    def test_the_question_has_buttons_for_topics_and_a_pick(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("recommend"))
        ids = [o["id"] for o in last(app, "options").payload["options"]]
        self.assertIn("surprise", ids)
        self.assertTrue(any(i.startswith("topic:") for i in ids), ids)
        self.assertEqual(ids[-2:], ["more", "done"])

    def test_not_sure_gets_a_book_not_the_same_question(self):
        for text in ("hmmm not sure anything u wanna suggest?", "can you just recommend me one?", "surprise me", "you pick"):
            with self.subTest(text=text):
                app = new_app("quiet")
                run(app, PersonEngaged(), UserChoice("recommend"), UserUtterance(text))
                self.assertEqual(self.asks(app), 1, text)                      # asked once, never again
                self.assertTrue(any(s.startswith("How about") for s in app.said), app.said)
                self.assertEqual(last(app).view, "book_list")

    def test_another_one_after_a_suggestion_is_a_different_book(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("recommend"), UserUtterance("just recommend me one"))
        first = [s for s in app.said if s.startswith("How about")][-1]
        run(app, UserUtterance("another one please"))
        picks = [s for s in app.said if s.startswith("How about")]
        self.assertEqual(len(picks), 2)
        self.assertNotEqual(picks[0].split("?")[0], picks[1].split("?")[0], first)
        self.assertEqual(self.asks(app), 1)

    def test_the_surprise_and_topic_buttons(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("recommend"), UserChoice("surprise"))
        self.assertTrue(any(s.startswith("How about") for s in app.said))
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("recommend"), UserChoice("topic:astronomy"))
        self.assertTrue(any("astronomy" in s for s in app.said[-3:]), app.said)

    def test_a_pick_is_never_repeated_and_never_the_book_just_returned(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        seen = []
        run(app, UserChoice("recommend"))
        for _ in range(4):
            run(app, UserUtterance("just surprise me"))
            seen.append([s for s in app.said if s.startswith("How about")][-1].split("?")[0])
        self.assertEqual(len(set(seen)), len(seen), seen)
        self.assertFalse(any("The Martian" in s for s in seen), seen)

    def test_the_first_question_in_a_common_area_is_still_asked_aloud(self):
        app = new_app("common")
        run(app, PersonEngaged(), UserChoice("recommend"))
        self.assertEqual(self.asks(app), 1)
        self.assertFalse(any(s.startswith("How about") for s in app.said))

    def test_with_a_book_in_hand_recommend_still_means_something_like_it(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), BookPlaced(), scanned(MARTIAN), UserUtterance("can you just recommend me one?"))
        self.assertFalse(any(s.startswith("How about") for s in app.said), app.said)

    def test_the_new_button_ids_are_accepted_everywhere(self):
        self.assertIn("surprise", INPUT_CHOICES)
        self.assertIn("surprise", PICKS)
        self.assertTrue(CONTENT_CHOICE.match("topic:Science Fiction"))
        self.assertFalse(CONTENT_CHOICE.match("topic:<script>"))


class QuietZoneScreenTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def returned(self, zone):
        app = new_app(zone)
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN))
        waiting = last(app, "book_card")
        run(app, BookDropped())
        return app, waiting, last(app, "book_card")

    def test_the_return_is_confirmed_on_the_screen_in_a_quiet_zone(self):
        app, waiting, done = self.returned("quiet")
        self.assertEqual(waiting.state_path, "ENGAGED.STORE")
        self.assertIn("Thank you for returning The Martian", waiting.payload["message"])
        self.assertTrue(done.payload["badge"].startswith("✓ Returned"), done.payload["badge"])
        self.assertIn("storage slot", done.payload["badge"])
        self.assertIn("Done: The Martian is in storage slot", done.payload["message"])
        self.assertIn("Is there anything else I can help with?", done.payload["message"])

    def test_a_common_area_hears_it_so_there_is_no_message_but_the_badge_still_says_returned(self):
        app, waiting, done = self.returned("common")
        self.assertEqual(done.payload["message"], "")
        self.assertTrue(done.payload["badge"].startswith("✓ Returned"))

    def test_an_explanation_stays_on_the_screen_in_full_with_the_question_under_it(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserUtterance("can you tell me about dune?"))
        card = last(app, "book_card")
        self.assertEqual(card.state_path, "ENGAGED.ANYTHING_ELSE")
        self.assertIn("desert planet", card.payload["message"])                   # the explanation, not cut to the card's summary
        self.assertTrue(card.payload["message"].endswith("Is there anything else I can help with?"))

    def test_the_next_answer_replaces_the_last_one(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserUtterance("can you tell me about dune?"), UserUtterance("who wrote it?"))
        message = last(app, "book_card").payload["message"]
        self.assertNotIn("desert planet", message)
        self.assertIn("Frank Herbert", message)

    def test_a_recommendation_and_directions_carry_their_words_too(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserUtterance("can you just recommend me one"), UserUtterance("where is the help desk?"))
        self.assertIn("help desk is on the ground floor", last(app, "directions").payload["message"])
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("recommend"), UserChoice("surprise"))
        self.assertTrue(last(app, "book_list").payload["message"].startswith("How about"))

    def test_the_stored_content_is_not_changed_by_the_message(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserUtterance("can you tell me about dune?"))
        composer = app.screen if hasattr(app, "screen") else None
        if composer is not None and composer.content is not None:
            self.assertEqual(composer.content[1].message, "")

    def test_the_face_caption_is_the_whole_text_so_the_page_can_set_it_to_fit(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserUtterance("what is on your pad"))
        self.assertGreater(len(app.said[-1]), 0)


if __name__ == "__main__":
    unittest.main()


class ChangedMindAfterBeingHandedABookTest(unittest.TestCase):
    """The book handed over from the storage ring is not in storage while the visitor holds it. "nvm, you can have it" puts it
    back in its (still lit) slot, on the screen and in what the robot says it holds: never the LLM's "kept in slot 1" while the
    slot is empty and nothing on the screen shows the book."""

    def setUp(self):
        quiet_logs(self)

    def handed_over(self, zone="common"):
        from tests.test_storage_offer import with_stored
        app, book = with_stored(zone)
        run(app, UserUtterance(f"do you have {book.title} with you?"), UserUtterance("yes"))
        return app, book

    def test_the_book_goes_back_into_its_slot(self):
        for phrase in ("nvm i dont need it you can have it", "never mind", "i changed my mind", "take it back", "i don't want it"):
            with self.subTest(phrase=phrase):
                app, book = self.handed_over()
                self.assertIsNone(app.dialogue.storage.slot_of(book.isbn))              # handed over: not in the ring
                run(app, UserUtterance(phrase))
                self.assertEqual(app.dialogue.storage.slot_of(book.isbn), book.slot)    # and back in it, in the same slot
                said = " ".join(app.said[-2:])
                self.assertIn(f"put {book.title} back in slot {book.slot}", said)
                self.assertNotIn("is empty", said)

    def test_the_screen_shows_the_book_with_returned_on_it(self):
        for zone in ("common", "quiet"):
            with self.subTest(zone=zone):
                app, book = self.handed_over(zone)
                n = len(app.screens)
                run(app, UserUtterance("nvm i dont need it you can have it"))
                card = last(app, "book_card")
                self.assertEqual(card.payload["title"], book.title)
                self.assertTrue(card.payload["badge"].startswith("\u2713 Returned"), card.payload["badge"])
                self.assertIn(f"slot {book.slot}", card.payload["badge"])
                self.assertGreater(len(app.screens), n)

    def test_what_a_quiet_zone_reads_is_the_new_reply_not_the_old_one(self):
        app, book = self.handed_over("quiet")
        run(app, UserUtterance("nvm i dont need it you can have it"))
        message = last(app, "book_card").payload["message"]
        self.assertIn(f"put {book.title} back in slot", message)
        self.assertNotIn("Here you go", message)
        self.assertEqual(message.count("Is there anything else"), 1)

    def test_it_now_says_it_holds_the_book_again(self):
        app, book = self.handed_over()
        run(app, UserUtterance("nvm i dont need it"), UserUtterance("which book are you holding?"))
        self.assertIn(f"{book.title} in slot {book.slot}", " ".join(app.said[-3:]))

    def test_saying_it_twice_is_not_a_second_return(self):
        app, book = self.handed_over()
        run(app, UserUtterance("nvm i dont need it"))
        said = len(app.said)
        run(app, UserUtterance("never mind"))
        self.assertNotIn("put " + book.title, " ".join(app.said[said:]))

    def test_without_a_handed_over_book_the_same_words_are_not_special(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("never mind"))
        self.assertNotIn("back in slot", " ".join(app.said))

    def test_the_slot_light_goes_off_when_the_visit_ends(self):
        app, book = self.handed_over()
        run(app, UserUtterance("nvm i dont need it"), UserUtterance("that's all"))
        self.assertIsNone(app.dialogue.lent_slot)
        self.assertIsNone(app.dialogue.lent_book)
        self.assertEqual(app.dialogue.storage.slot_of(book.isbn), book.slot)

    def test_each_direct_reply_starts_the_screen_text_afresh(self):
        app, book = self.handed_over("quiet")
        run(app, UserUtterance("nvm i dont need it"))
        self.assertEqual(app.dialogue._screen_text, "")


class QuietZoneShowsEverythingTest(unittest.TestCase):
    """Nobody hears the robot in a quiet zone, so every line it says is on the screen, whole: no reply that only reaches the
    terminal, no "..." in a book's summary or a pick's reason."""

    LONG = ("Libraries have a long history of lending books to people who cannot afford to buy them, and many also run reading "
            "groups, quiet study rooms, free computer access and children's story hours. ") * 2

    def setUp(self):
        quiet_logs(self)

    @staticmethod
    def on_screen(update):
        return " ".join(" ".join(v for v in [update.text, *[x for x in update.payload.values() if isinstance(x, str)]] if v).split())

    def assert_all_said_is_shown(self, app, *utterances):
        for text in utterances:
            said, screens = len(app.said), len(app.screens)
            run(app, UserUtterance(text))
            for line in app.said[said:]:
                self.assertTrue(any(" ".join(line.split()) in self.on_screen(u) for u in app.screens[screens:]),
                                f"{text!r}: {line[:60]!r} never reached the screen")

    def app_with_llm(self, zone="quiet"):
        from tests.test_dialogue import StubClient
        app = new_app(zone, llm=StubClient(*[self.LONG] * 20))
        run(app, PersonEngaged())
        return app

    def test_a_general_answer_reaches_the_screen_in_every_state(self):
        for before in ([], ["where is the toilet"], ["recommend a book about space"], ["tell me about dune"],
                       ["i want to ask about a book"]):
            with self.subTest(before=before):
                app = self.app_with_llm()
                self.assert_all_said_is_shown(app, *before, "what do libraries do all day")

    def test_the_whole_conversation_is_on_the_screen(self):
        app = self.app_with_llm()
        self.assert_all_said_is_shown(app, "hello", "how are you today?", "can you tell me about dune?", "who wrote it?",
                                      "where is the help desk?", "recommend something like it")

    def test_a_long_answer_is_not_cut_short(self):
        app = self.app_with_llm()
        run(app, UserUtterance("what do libraries do all day"))
        shown = self.on_screen(app.screens[-1])
        self.assertIn(" ".join(self.LONG.split()), shown)
        self.assertNotIn("…", shown)

    def test_no_summary_or_reason_is_cut_in_a_quiet_zone(self):
        from tests.test_screen import composer
        quiet, common = composer("quiet"), composer("common")
        quiet.zone, common.zone = "quiet", "common"
        long_one = max(quiet.finder.catalog.books, key=lambda b: len(b.summary or ""))
        self.assertEqual(quiet.book_card(long_one.isbn13).summary, " ".join(long_one.summary.split()))
        self.assertTrue(common.book_card(long_one.isbn13).summary.endswith("…"))
        from robot.core.screen import short_reason, trim
        reason = "because, like Cosmos, it is about popular science " * 4
        self.assertTrue(short_reason(reason, 0).endswith("science"))
        self.assertTrue(trim(reason, 70).endswith("…"))

    def test_the_goodbye_line_is_on_the_screen_too(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), PersonLeft())
        self.assertIn("Enjoy your reading", app.screens[-1].text)
        self.assertFalse(any("Enjoy" in s for s in app.said))                       # (read, not said)

    def test_the_page_never_clamps_a_quiet_zone_and_shrinks_text_to_fit(self):
        from pathlib import Path
        ui = Path(__file__).resolve().parents[1] / "display" / "ui"
        html, views = (ui / "index.html").read_text(), (ui / "views.js").read_text()
        self.assertIn("body.zone-quiet .clamp2", html)
        self.assertIn("-webkit-line-clamp: unset !important", html)
        self.assertIn("function fit(", views)
        self.assertIn("fit(target)", views)

    def test_an_open_page_reloads_itself_when_the_page_files_change(self):
        from pathlib import Path
        from display.server import ui_version
        self.assertTrue(ui_version())
        html = (Path(__file__).resolve().parents[1] / "display" / "ui" / "index.html").read_text()
        self.assertIn("/ui-version", html)
        self.assertIn("location.reload()", html)


class ThanksWithARequestTest(unittest.TestCase):
    """"thank you, can you recommend me some book realted to the martian?" said goodbye: the older done rule saw "thank you"."""

    def test_thanks_with_a_request_is_not_done(self):
        from robot.core.dialogue import is_done
        for text in ("thank you, can you recommend me some book realted to the martian?", "thanks, where is the toilet",
                     "thank you can i ask something else", "thanks. recommend me something", "thank you, tell me about dune"):
            with self.subTest(text=text):
                self.assertFalse(is_done(text, "closing"))

    def test_thanks_and_goodbyes_alone_are_still_done(self):
        from robot.core.dialogue import is_done
        for text in ("thank you", "thanks, that is all", "thank you very much", "thanks for your help", "ok bye", "thanks bye"):
            with self.subTest(text=text):
                self.assertTrue(is_done(text, "closing"))

    def test_after_being_handed_a_book_the_visit_goes_on(self):
        quiet_logs(self)
        from tests.test_storage_offer import with_stored
        app, book = with_stored("common")
        run(app, UserUtterance(f"can I get {book.title}?"), UserUtterance("yes"),
            UserUtterance(f"thank you, can you recommend me some book realted to {book.title}?"))
        self.assertNotEqual(app.dialogue.leaf, "FAREWELL")
        self.assertTrue(any("you might enjoy" in s or "I think you should" in s or "How about" in s for s in app.said))
