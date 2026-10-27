"""Follow-up questions about the book just recommended, no yes/no/guide buttons, and the toilets, help desk and exit."""

import asyncio
import unittest

from robot.events import PersonEngaged, UserChoice, UserUtterance, BookPlaced
from robot.services.floorplan import FloorPlan, FloorPlanError
from robot.services.places import PlaceResolver
from tests.test_dialogue import HITCH, MARTIAN, scanned
from tests.test_screen import COSMOS, composer, enter
from tests.test_state_machine import new_app, quiet_logs, run


def ask(app, *texts):
    """What the robot said in answer to the last of the texts."""
    run(app, *[UserUtterance(t) for t in texts[:-1]]) if len(texts) > 1 else None
    before = len(app.said)
    run(app, UserUtterance(texts[-1]))
    return " ".join(app.said[before:])


class FollowUpTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def recommended(self, zone="common"):
        app = new_app(zone)
        run(app, PersonEngaged(), UserUtterance("i dont have any book with me but maybe science fictions?"))
        self.assertIn("A Brief History of Time", " ".join(app.said))           # the top pick (quiet zones name one book, common three)
        return app

    def test_that_book_after_a_recommendation_is_the_one_recommended(self):
        for zone in ("common", "quiet"):
            for follow, expect in (("can you explain what that book is about?", "An accessible tour"),
                                   ("tell me about that one", "An accessible tour"),
                                   ("what is it about", "An accessible tour"),
                                   ("who wrote it?", "Stephen Hawking"),
                                   ("where is that?", "3-SCI-A"),
                                   ("where is it located", "3-SCI-A")):
                with self.subTest(zone=zone, follow=follow):
                    app = self.recommended(zone)
                    said = ask(app, follow)
                    self.assertIn(expect, said)
                    self.assertNotIn("Which book", said)
                    self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_the_recommended_book_becomes_the_topic(self):
        app = self.recommended()
        self.assertEqual(app.dialogue.session.book.title, "A Brief History of Time")
        self.assertTrue(app.dialogue.session.virtual)             # named, not on the pad: it cannot be returned

    def test_the_book_on_the_pad_stays_the_topic_but_that_one_means_the_recommendation(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("recommend"), BookPlaced(), scanned(HITCH))
        s = app.dialogue.session
        self.assertEqual(s.book.isbn, HITCH)
        self.assertFalse(s.virtual)
        self.assertTrue(s.focus and s.focus != HITCH)
        said = ask(app, "explain that book")
        recommended = app.finder.catalog.lookup(s.focus)
        self.assertIn(recommended.title, said)

    def test_a_new_session_forgets_it(self):
        app = self.recommended()
        run(app, UserUtterance("that's all, thanks"))
        run(app, PersonEngaged())
        self.assertEqual(app.dialogue.session.focus, "")


class NoButtonsTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_anything_else_has_no_options(self):
        for zone in ("quiet", "common"):
            app = new_app(zone)
            run(app, PersonEngaged(), UserUtterance("hello"))
            self.assertEqual(app.dialogue.options_for("ANYTHING_ELSE", True), [])
            self.assertEqual(app.dialogue.options_for("ANYTHING_ELSE"), [])

    def test_the_screen_is_the_face_with_the_question(self):
        for zone in ("quiet", "common"):
            c = composer(zone)
            update = enter(c, "ENGAGED.ANYTHING_ELSE", zone, "Is there anything else I can help with?")
            self.assertEqual((update.view, update.layout, update.options), ("face", "full_face", ()))
            self.assertTrue(update.payload["caption"])

    def test_no_guide_me_button_on_the_directions(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where are the science books"))
        shown = [u for u in app.screens if u.view == "directions"][-1]
        self.assertEqual(shown.payload["buttons"], [])


class FacilityTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_answers_come_from_the_floor_plan(self):
        cases = {"where is the toilet": ("The toilets are on the ground floor", "level 2 and level 3"),
                 "can you tell me where the toilets are": ("The toilets are", "next to the lift"),
                 "where's the help desk": ("The help desk is on the ground floor", "near the entrance"),
                 "where is the exit?": ("The exit is on the ground floor", "next to the entrance"),
                 "how do I get to the entrance": ("The entrance is", "from here")}
        for zone in ("common", "quiet"):
            for text, expect in cases.items():
                with self.subTest(zone=zone, text=text):
                    app = new_app(zone)
                    run(app, PersonEngaged())
                    said = ask(app, text)
                    for part in expect:
                        self.assertIn(part, said)
                    self.assertNotIn("guide you", said)
                    self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_other_ways_of_asking_and_typos(self):
        for text, expect in (("whre is the tolet", "toilets"), ("I need the restroom", "toilets"), ("is there a bathroom here", "toilets"),
                             ("where can I find the information desk", "help desk"), ("show me the way out", "exit"),
                             ("where is the loo", "toilets"), ("the toilets please", "toilets")):
            with self.subTest(text=text):
                app = new_app()
                run(app, PersonEngaged())
                self.assertIn(expect, ask(app, text).lower())

    def test_a_bare_exit_is_not_a_question_about_the_exit(self):
        app = new_app()
        run(app, PersonEngaged())
        self.assertNotIn("The exit is", ask(app, "exit"))

    def test_it_works_whatever_the_robot_is_talking_about(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("tell me about cosmos"))
        said = ask(app, "can you tell me where the toilet is")
        self.assertIn("The toilets are", said)
        self.assertNotIn("black holes", said)                       # not a question about the book
        self.assertNotIn("Let me check", said)

    def test_the_screen_shows_the_map_with_the_toilets(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where is the toilet"))
        shown = [u for u in app.screens if u.view == "directions"][-1].payload
        self.assertEqual(shown["steps"], ["Go to the toilets"])
        self.assertEqual((shown["target"]["x"], shown["target"]["y"]), (10.8, 5.0))
        self.assertEqual(shown["buttons"], [])

    def test_the_nearest_floor_is_told_first(self):
        app = new_app()
        plan = app.dialogue.plan
        self.assertEqual([f.level for f, _ in plan.find("toilets")], [1, 2, 3])
        self.assertEqual([f.level for f, _ in plan.find("exit")], [1])
        self.assertEqual(PlaceResolver(app.finder, plan).facility_place("toilets").floor, 1)

    def test_a_named_floor_answers_for_that_floor_with_the_lift_first(self):
        for zone in ("common", "quiet"):
            for text in ("where are the toilets on level 3", "is there a toilet on level 3"):
                with self.subTest(zone=zone, text=text):
                    app = new_app(zone)
                    run(app, PersonEngaged())
                    said = ask(app, text)
                    self.assertIn("The toilets are on level 3", said)
                    self.assertIn("ground floor", said)                      # and where else they are
                    shown = [u for u in app.screens if u.view == "directions"][-1].payload
                    self.assertEqual(shown["steps"], ["Take the lift to Level 3", "Then go to the toilets"])
                    self.assertEqual((shown["level"], shown["other_floor"]), (3, True))

    def test_a_floor_without_one_says_so_and_gives_the_nearest(self):
        app = new_app()
        run(app, PersonEngaged())
        said = ask(app, "where is the help desk on level 2")
        self.assertIn("There is no help desk on level 2", said)
        self.assertIn("The help desk is on the ground floor", said)

    def test_without_a_landmark_it_says_so_and_points_to_the_help_desk(self):
        app = new_app()
        floors = {lvl: f for lvl, f in app.dialogue.plan.floors.items()}
        for lvl, f in floors.items():
            floors[lvl] = type(f)(f.level, f.name, f.width, f.height, f.sections,
                                  tuple(m for m in f.landmarks if m.kind != "toilets"))
        app.dialogue.plan.floors = floors
        run(app, PersonEngaged())
        said = ask(app, "where is the toilet")
        self.assertIn("don't have the toilets on my map", said)
        self.assertIn("help desk", said)

    def test_exit_is_a_landmark_kind(self):
        data = {"floors": [{"level": 1, "width": 5, "height": 5, "sections": [],
                            "landmarks": [{"kind": "exit", "name": "Exit", "x": 1, "y": 1}]}]}
        self.assertEqual(FloorPlan.from_dict(data).find("exit")[0][1].name, "Exit")
        data["floors"][0]["landmarks"][0]["kind"] = "portal"
        with self.assertRaises(FloorPlanError):
            FloorPlan.from_dict(data)


if __name__ == "__main__":
    unittest.main()
