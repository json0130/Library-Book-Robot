"""The intent router that runs before the LLM: every intent, speech errors and typos, the guards against near-misses, and
what the dialogue does with a close call (ask), a miss (the LLM) and a second miss in a row (repair)."""

import logging
import unittest

from robot.core.intents import edit_cost, normalise, word_similarity
from robot.events import PersonEngaged, ScreenUpdate, UserUtterance
from tests.test_dialogue import StubClient, make_app, run
from tests.test_state_machine import new_app, quiet_logs


def router():
    logging.disable(logging.INFO)
    return make_app().dialogue.router


class NormaliseTest(unittest.TestCase):
    def test_lower_case_no_punctuation_contractions_expanded(self):
        self.assertEqual(normalise("I'm DONE!"), "i am done")
        self.assertEqual(normalise("What's on level 2?"), "what is on level 2")
        self.assertEqual(normalise("that's all, thanks."), "that is all thanks")
        self.assertEqual(normalise("Sci-Fi"), "sci fi")

    def test_edit_cost_counts_a_swap_as_half(self):
        self.assertEqual(edit_cost("book", "book"), 0)
        self.assertEqual(edit_cost("bok", "book"), 1)
        self.assertEqual(edit_cost("sceince", "science"), 0.5)

    def test_short_words_must_be_nearly_exact(self):
        self.assertGreaterEqual(word_similarity("science", "sceince"), 0.8)
        self.assertLess(word_similarity("anything", "nothing"), 0.9)


class RouterIntentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = router()

    def best(self, text, state="ANYTHING_ELSE", **kw):
        return self.router.route(text, state, **kw).best

    def test_every_intent_has_a_plain_phrase(self):
        cases = [("I'm done", "done"), ("that's all thanks", "done"), ("no thanks", "done"), ("nothing else", "done"),
                 ("yes please", "yes"), ("I want to return a book", "return"), ("tell me about it", "explain"),
                 ("what is this book about", "explain"), ("recommend something similar", "recommend"),
                 ("take me there", "guide"), ("where is it", "where")]
        for text, intent in cases:
            with self.subTest(text=text):
                self.assertEqual(self.best(text, has_book=True).intent, intent)

    def test_recommend_with_an_interest_names_the_interest(self):
        for text, interest in (("a good sci-fi book", "sci fi"), ("something about space", "space")):
            with self.subTest(text=text):
                best = self.best(text, "AWAIT_BOOK")
                self.assertEqual(best.intent, "recommend")
                self.assertEqual(best.interest, interest)

    def test_sections_floors_and_titles(self):
        self.assertEqual((self.best("the computing section").intent, self.best("the computing section").place.section), ("where", "Computing"))
        self.assertEqual(self.best("science books").place.section, "Science")
        self.assertEqual(self.best("level 2").intent, "where")
        title = self.best("A Brief History of Time")
        self.assertEqual(title.intent, "where")
        self.assertTrue(title.isbn)

    def test_typos_and_speech_errors_still_route(self):
        cases = [("um where is the sceince secton", "where"), ("wear is the computing section", "where"),
                 ("im done", "done"), ("thats all thanks", "done"), ("retrun a book", "return"),
                 ("tel me about it", "explain")]
        for text, intent in cases:
            with self.subTest(text=text):
                best = self.best(text, has_book=True)
                self.assertIsNotNone(best, text)
                self.assertEqual(best.intent, intent)

    def test_anything_else_is_never_nothing_else(self):
        self.assertEqual(self.best("nothing else").intent, "done")
        self.assertIsNone(self.best("anything else"))
        self.assertIsNone(self.best("anything else please"))
        for typo in ("nothin else", "anythng else"):
            best = self.best(typo)
            self.assertTrue(best is None or best.intent != "done" or typo.startswith("nothin"), typo)

    def test_a_short_word_close_to_a_title_is_not_that_title(self):
        self.assertIsNone(self.best("im dun"))                       # "dun" is not "Dune"
        self.assertEqual(self.best("dune").intent, "title")

    def test_a_word_split_in_two_is_put_back_together(self):
        self.assertEqual(self.router.route("um his story", "ANYTHING_ELSE").norm, "um history")
        self.assertEqual(self.best("sci ence books").place.section, "Science")
        self.assertIsNone(self.best("toy story"))                     # both words mean something alone: left as they are

    def test_naming_more_of_the_title_than_the_section_is_not_ambiguous(self):
        route = self.router.route("where is the brief history", "ANYTHING_ELSE")
        self.assertEqual(route.best.place.kind, "book")
        self.assertFalse(route.ambiguous)

    def test_guide_phrases_survive_a_dropped_word_and_a_slip_on_done(self):
        self.assertEqual(self.best("okay show me way").intent, "guide")
        self.assertEqual(self.best("I'm dine").intent, "done")
        self.assertIsNone(self.best("bone"))

    def test_a_confident_match_for_the_state_is_one_clear_winner(self):
        route = self.router.route("where is the computing section", "ANYTHING_ELSE")
        self.assertFalse(route.ambiguous)
        self.assertGreaterEqual(route.best.confidence, 0.7)

    def test_two_close_places_are_ambiguous(self):
        route = self.router.route("history", "ANYTHING_ELSE")
        self.assertEqual({c.label for c in route.ambiguous}, {"section History", "title A Brief History of Time"})

    def test_unknown_questions_have_no_candidates_so_the_llm_gets_them(self):
        for text in ("how tall is the Eiffel tower", "blah blah", "what is the meaning of life", ""):
            with self.subTest(text=text):
                self.assertIsNone(self.best(text))


class DialogueRoutingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_router_answers_without_calling_the_llm(self):
        llm = StubClient()
        app = new_app("common", llm=llm)
        run(app, PersonEngaged(), UserUtterance("the computing section"))
        self.assertEqual(llm.prompts, [])
        self.assertIn("directions", [s.view for s in app.screens])

    def test_a_close_call_asks_which_one_with_an_options_view(self):
        app = new_app("common")
        run(app, PersonEngaged(), UserUtterance("history"))
        options = [s for s in app.screens if s.view == "options"][-1]
        labels = [o["label"] for o in options.payload["options"]]
        self.assertTrue(any("History" in l for l in labels), labels)
        self.assertTrue(any("Brief History" in l for l in labels), labels)

    def test_a_question_nothing_matches_goes_to_the_llm(self):
        llm = StubClient("It is about 330 metres tall, as far as I know.")
        app = new_app("common", llm=llm)
        run(app, PersonEngaged(), UserUtterance("how tall is the Eiffel tower"))
        self.assertEqual(len(llm.prompts), 1)


class RepairTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def gibberish(self, app, n):
        for text in ("flurb glorp", "zxqv wibble", "blorp snerk")[:n]:
            run(app, UserUtterance(text))

    def test_the_second_miss_in_a_row_shows_the_options_with_im_done(self):
        for zone in ("quiet", "common"):
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged())
                before = len(app.screens)
                self.gibberish(app, 1)
                self.assertFalse(app.dialogue.session.repair)
                self.gibberish(app, 2)
                shown = [s for s in app.screens[before:] if s.view == "options"]
                self.assertTrue(shown)
                labels = [o["label"] for o in shown[-1].payload["options"]]
                self.assertTrue(any("done" in l.lower() for l in labels), labels)

    def test_only_the_common_zone_says_the_options(self):
        quiet, common = new_app("quiet"), new_app("common")
        for app in (quiet, common):
            run(app, PersonEngaged())
            app.said.clear()
            self.gibberish(app, 2)
        self.assertTrue(any("You can say" in s for s in common.said), common.said)
        self.assertFalse(any("You can say" in s for s in quiet.said), quiet.said)

    def test_understood_utterances_reset_the_count(self):
        app = new_app("common")
        run(app, PersonEngaged())
        self.gibberish(app, 1)
        run(app, UserUtterance("where is the computing section"))
        self.assertEqual(app.dialogue.session.misses, 0)

    def test_a_question_the_robot_cannot_answer_is_not_a_failure(self):
        llm = StubClient(*["I am not sure about that."] * 3)
        app = new_app("common", llm=llm)
        run(app, PersonEngaged())
        for _ in range(3):
            run(app, UserUtterance("how tall is the Eiffel tower?"))
        self.assertFalse(app.dialogue.session.repair)
        self.assertEqual(app.dialogue.session.misses, 0)
