"""Expectation-aware matching (robot/core/expectations.py): right after a question with a few answers, a short reply is
matched against only those answers, loosely; with no question pending the same words do nothing."""

import unittest

from robot.core.expectations import (expect_anything_else, expect_choice, expect_guide, match_answers, words_of)
from robot.events import NavigationRequested, PersonEngaged, UserUtterance
from tests.test_dialogue import run
from tests.test_state_machine import new_app, quiet_logs

GARBLED = ("guy me", "er tqke me thre", "yes plz")


def fresh(zone="common"):
    app = new_app(zone)
    app.navigation = []
    app.bus.subscribe(NavigationRequested, app.navigation.append)
    run(app, PersonEngaged())
    return app


def offered(zone="common"):
    """The robot has just said where Dune is and offered to guide (a guide expectation is pending)."""
    app = fresh(zone)
    run(app, UserUtterance("where is dune"))
    assert "guide you" in app.said[-1], app.said
    return app


class MatcherTest(unittest.TestCase):
    def test_the_garbled_replies_match_the_guide_offer(self):
        for text, phrase in (("guy me", "guide me"), ("er tqke me thre", "take me there"), ("yes plz", "yes"),
                             ("gide me", "guide me"), ("lead the way", "lead the way"), ("show me", "show me"),
                             ("shwo me the way", "show me the way")):
            with self.subTest(text=text):
                found = match_answers(text, expect_guide(), {})
                self.assertEqual((found.answer, found.phrase), ("guide", phrase))

    def test_a_no_to_the_offer_is_done(self):
        for text in ("no", "nope", "no thanks", "nothing else", "nohing else", "thats all"):
            with self.subTest(text=text):
                self.assertEqual(match_answers(text, expect_guide(), {}).answer, "done")

    def test_anything_else_and_nothing_else_stay_apart(self):
        for expectation in (expect_anything_else(), expect_guide()):
            self.assertEqual(match_answers("nothing else", expectation, {}).answer, "done")
            self.assertIsNone(match_answers("anything else", expectation, {}))           # never turned into "nothing else"
            self.assertIsNone(match_answers("anyting else", expectation, {}))
        self.assertEqual(match_answers("yes plz", expect_anything_else(), {}).answer, "more")
        self.assertEqual(match_answers("another book", expect_anything_else(), {}).answer, "another")

    def test_other_words_are_left_to_the_router(self):
        for text in ("show me dune", "who wrote it", "where is the toilet", "tell me more", "me", "now", "history",
                     "take me to the science section please", "guide me to the exit now"):
            with self.subTest(text=text):
                self.assertIsNone(match_answers(text, expect_guide(), {}))

    def test_longer_replies_are_not_matched(self):
        self.assertIsNotNone(match_answers("er yes plz", expect_guide(), {}))                 # fillers are not counted
        self.assertIsNone(match_answers("guy me to the place", expect_guide(), {}))
        self.assertIsNone(match_answers("guy me", expect_guide(), {"expected_max_words": 1}))  # the limit is a setting

    def test_the_threshold_is_a_setting_separate_from_the_global_one(self):
        strict = {"expected_word_similarity": 0.9, "expected_phrase_similarity": 0.95}
        self.assertIsNone(match_answers("er tqke me thre", expect_guide(), strict))
        self.assertIsNotNone(match_answers("er tqke me thre", expect_guide(), {"expected_word_similarity": 0.65}))
        self.assertEqual(words_of("er tqke me thre"), ["tqke", "me", "thre"])

    def test_options_by_label(self):
        options = expect_choice([("section:History", "The History section"), ("where:1", "A Brief History of Time")])
        self.assertEqual(match_answers("histroy section", options, {}).answer, "section:History")
        self.assertEqual(match_answers("brief", options, {}).answer, "where:1")
        self.assertIsNone(match_answers("history", options, {}))                              # fits both: the router asks again
        self.assertIsNone(match_answers("dune", options, {}))


class DialogueTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_garbled_replies_after_the_guide_offer_guide(self):
        for zone in ("common", "quiet"):
            for text in GARBLED:
                with self.subTest(zone=zone, text=text):
                    app = offered(zone)
                    run(app, UserUtterance(text))
                    self.assertEqual([e.title for e in app.navigation], ["Dune"])
                    self.assertIn("follow me to Dune", " ".join(app.said))

    def test_the_same_words_with_no_pending_question_do_not_guide(self):
        for text in GARBLED:
            with self.subTest(text=text):
                app = fresh()                                           # OFFER: it asked what they want, not whether to guide
                run(app, UserUtterance(text))
                self.assertEqual(app.navigation, [])
                self.assertNotIn("follow me", " ".join(app.said))
                app = fresh()
                run(app, UserUtterance("tell me about dune"))            # an answer, then "anything else?": no guide offered
                self.assertNotIn("guide you", " ".join(app.said))
                run(app, UserUtterance(text))
                self.assertEqual(app.navigation, [])

    def test_the_expectation_lasts_for_one_utterance(self):
        app = offered()
        run(app, UserUtterance("hmm what about its author"))             # something else: the expectation is used up
        self.assertEqual(app.navigation, [])
        run(app, UserUtterance("guy me"))
        self.assertEqual(app.navigation, [])

    def test_a_button_ends_the_expectation_too(self):
        from robot.events import UserChoice
        app = offered()
        run(app, UserChoice("more"))
        self.assertIsNone(app.dialogue.session.expect)

    def test_it_does_not_outlive_the_visit(self):
        app = offered()
        self.assertIsNotNone(app.dialogue.session.expect)
        run(app, UserUtterance("thats all"))
        self.assertIsNone(app.dialogue.session.expect)

    def test_nothing_else_ends_the_visit_and_anything_else_does_not(self):
        app = fresh()
        run(app, UserUtterance("tell me about dune"))
        self.assertEqual(app.dialogue.session.expect.kind, "anything_else")
        run(app, UserUtterance("nohing else"))
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
        app = fresh()
        run(app, UserUtterance("tell me about dune"))
        run(app, UserUtterance("anything else"))
        self.assertNotEqual(app.dialogue.state, "ENGAGED.FAREWELL")

    def test_yes_plz_to_anything_else_is_more_help(self):
        app = fresh()
        run(app, UserUtterance("tell me about dune"))
        run(app, UserUtterance("yes plz"))
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_a_garbled_choice_after_did_you_mean(self):
        app = fresh()
        run(app, UserUtterance("where is the brief history"))
        run(app, UserUtterance("history"))                                 # asks: the section or the book
        self.assertIn("Did you mean", " ".join(app.said))
        run(app, UserUtterance("sectoin"))
        self.assertIn("History section", " ".join(app.said[-3:]))


if __name__ == "__main__":
    unittest.main()
