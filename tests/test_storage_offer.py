"""A book the robot is holding in its storage is offered ("Would you like it?"), not located on a shelf with a guide offer;
and after a return the screen goes back to the face alone."""

import unittest

from robot.events import BookDropped, BookPlaced, PersonEngaged, UserChoice, UserUtterance
from tests.test_dialogue import MARTIAN, advance, scanned
from tests.test_state_machine import new_app, quiet_logs, run


def with_stored(zone="common"):
    app = new_app(zone)
    app.dialogue.fill_storage(2)
    held = app.dialogue.storage.stored()
    run(app, PersonEngaged())
    return app, held[1]


def sent(app, command):
    return [params for cmd, params in app.actuators.lights.esp32.sent if cmd == command]


class OfferTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_it_offers_the_copy_it_holds_instead_of_a_guide_offer(self):
        for zone in ("common", "quiet"):
            for ask in ("where is {t}", "acutally do you ahve {t} with u?", "do you have {t}", "can i borrow {t}"):
                with self.subTest(zone=zone, ask=ask):
                    app, book = with_stored(zone)
                    run(app, UserUtterance(ask.format(t=book.title)))
                    said = " ".join(app.said)
                    self.assertIn(f"I have {book.title} in my storage, in slot {book.slot}. Would you like it?", said)
                    self.assertNotIn("guide you", said)
                    self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                    self.assertEqual(app.said[-1].count("anything else"), 0)          # the question is asked once

    def test_yes_hands_it_over_and_lights_its_slot(self):
        for yes in ("yes", "yes plz", "yep please", "sure"):
            with self.subTest(yes=yes):
                app, book = with_stored()
                run(app, UserUtterance(f"where is {book.title}"))
                run(app, UserUtterance(yes))
                self.assertIn(f"{book.title} is in slot {book.slot}", " ".join(app.said))
                self.assertIsNone(app.dialogue.storage.slot_of(book.isbn))          # it left the ring
                self.assertIn({"slot": book.slot}, sent(app, "ring"))
                self.assertIn({"slot": book.slot, "on": True}, sent(app, "slot_light"))
                self.assertEqual(app.said[-1], "Is there anything else I can help with?")
                run(app, UserUtterance("that's all"))
                self.assertIn({"slot": book.slot, "on": False}, sent(app, "slot_light"))      # the light goes off with the visit

    def test_no_keeps_it_and_says_where_it_goes(self):
        app, book = with_stored()
        run(app, UserUtterance(f"where is {book.title}"))
        run(app, UserUtterance("no thanks"))
        self.assertEqual(app.dialogue.storage.slot_of(book.isbn), book.slot)
        self.assertIn(f"shelf {book.shelf}", " ".join(app.said))

    def test_the_offer_is_for_the_next_utterance_only(self):
        app, book = with_stored()
        run(app, UserUtterance(f"where is {book.title}"))
        run(app, UserUtterance("who wrote it?"))
        run(app, UserUtterance("yes"))
        self.assertEqual(app.dialogue.storage.slot_of(book.isbn), book.slot)

    def test_a_book_that_is_not_stored_is_located_as_before(self):
        app, _ = with_stored()
        run(app, UserUtterance("where is cosmos"))
        self.assertIn("Would you like me to guide you there?", " ".join(app.said))
        self.assertNotIn("in my storage", " ".join(app.said))

    def test_the_book_just_handed_in_is_not_offered_back(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        run(app, UserUtterance("where is the martian"))
        said = " ".join(app.said)
        self.assertIn("shelf 2-SF-C", said)
        self.assertNotIn("Would you like it", said)


class FaceOnlyAfterReturnTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_after_a_return_and_done_the_screen_is_the_face_alone(self):
        for zone in ("common", "quiet"):
            for done in (lambda: UserUtterance("that's all"), lambda: UserChoice("done"), lambda: UserUtterance("im dun")):
                with self.subTest(zone=zone):
                    app = new_app(zone)
                    app.dialogue.settings.returning["auto_start"] = False       # as in config/robot.yaml
                    run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
                    self.assertEqual(app.screens[-1].view, "book_card")           # the slot card while it is stored
                    n = len(app.screens)
                    run(app, done())
                    advance(app, 60)
                    after = app.screens[n:]
                    self.assertTrue(after and all(u.view == "face" and u.layout == "full_face" for u in after), after)
                    self.assertEqual(app.screens[-1].view, "face")

    def test_with_auto_return_on_the_trip_shows_its_status_then_the_face(self):
        app = new_app()                                                          # new_app turns auto-return on
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped(), UserUtterance("that's all"))
        advance(app, 60)
        views = [u.view for u in app.screens if u.state_path.startswith("RETURNING")]
        self.assertIn("status", views)
        self.assertEqual(app.screens[-1].view, "face")
        self.assertEqual(app.dialogue.state, "IDLE")


if __name__ == "__main__":
    unittest.main()
