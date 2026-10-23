"""The screen composer, its views and payloads, the floor plan, place resolution, covers and the checker."""

import asyncio
import io
import json
import logging
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from display.server import Hub, allowed, make_server
from robot.config import ROOT
from robot.core.screen import (VIEWS, BookCardPayload, ScreenComposer, directions, l_path, payload_from_json, trim)
from robot.events import (PersonLeft, BookDropped, BookIdentified, BookPlaced, ChoicesOffered, LocationResolved,
                          NavigationRequested, PersonEngaged, RecommendationsReady, ReturningProgress, ScreenUpdate,
                          StateEntered, StoringBook, UserChoice, UserUtterance)
from robot.operator import Operator
from robot.services.book_graph import BookGraph, Shelf
from robot.services.books import Book, BookFinder, Catalog
from robot.services.covers import cover_colour, cover_file, download_cover, placeholder_svg
from robot.services.floorplan import FloorPlan, FloorPlanError
from robot.services.places import PlaceResolver
from robot.services.topics import TopicVocabulary
from tests.test_dialogue import HITCH, MARTIAN, advance, make_app, run, scanned

COSMOS, SAPIENS = "9780345539434", "9780062316097"
PLAN = ROOT / "data" / "floorplan.yaml"


def quiet_logs(test):
    logging.disable(logging.INFO)
    test.addCleanup(logging.disable, logging.NOTSET)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def composer(zone="common"):
    """A composer on the sample catalogue and floor plan, recording what it publishes."""
    app = make_app()
    shown = []

    async def publish(update):
        shown.append(update)

    clock = Clock()
    state = ["IDLE"]
    style = type("S", (), {"touch_buttons": zone == "quiet"})()
    c = ScreenComposer(publish, app.finder, FloorPlan.load(PLAN), {"content_ttl_s": {"quiet": 30, "common": 15}},
                       lambda: style, lambda: state[0], clock)
    c.shown, c.clock_, c.state = shown, clock, state
    return c


def enter(c, path, zone="common", text="", options=()):
    c.state[0] = path
    asyncio.run(c.on_state(StateEntered(path, zone, "", text, tuple(options))))
    return c.shown[-1]


def book_event(app, isbn):
    return scanned(isbn)


class ComposerMappingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    OFFER_OPTIONS = ({"id": "return", "label": "Return a book"}, {"id": "ask", "label": "Ask about a book"})

    def test_face_states_show_the_face_full_screen(self):
        for zone in ("common", "quiet"):
            c = composer(zone)
            for path in ("IDLE", "ENGAGED.GREET", "ENGAGED.FAREWELL", "RETURNING.PLAN"):
                update = enter(c, path, zone, "Hello")
                self.assertEqual((update.view, update.layout, update.ttl_s), ("face", "full_face", None), path)
            self.assertEqual(update.payload["caption"], "Hello" if zone == "quiet" else "")

    def test_offer_shows_the_choices_as_buttons_in_a_quiet_zone_and_the_face_in_a_common_one(self):
        quiet, common = composer("quiet"), composer("common")
        self.assertEqual(enter(quiet, "ENGAGED.OFFER", "quiet", "How can I help?", self.OFFER_OPTIONS).view, "options")
        self.assertEqual(quiet.shown[-1].payload["options"][0]["id"], "return")
        self.assertEqual(quiet.shown[-1].layout, "split")
        self.assertEqual(enter(common, "ENGAGED.OFFER", "common", "How can I help?").view, "face")

    def test_a_scanned_book_shows_its_card_in_the_offer(self):
        for zone in ("common", "quiet"):
            c = composer(zone)
            enter(c, "ENGAGED.SCAN", zone)
            self.assertEqual(c.shown[-1].view, "face")
            asyncio.run(c.on_book(scanned(COSMOS)))
            update = enter(c, "ENGAGED.OFFER", zone, "You have Cosmos", self.OFFER_OPTIONS)
            self.assertEqual(update.view, "book_card")
            card = update.payload
            self.assertEqual((card["title"], card["level"], card["section"], card["shelf"]), ("Cosmos", 3, "Science", "3-SCI-A"))
            self.assertEqual([b["id"] for b in card["buttons"]], [f"guide:{COSMOS}", f"similar:{COSMOS}"])
            self.assertTrue(card["cover_url"].startswith(f"/covers/{COSMOS}?"))

    def test_one_state_shows_different_views_by_content(self):
        c = composer()
        enter(c, "ENGAGED.ANSWER")
        asyncio.run(c.on_focus(type("F", (), {"isbn": COSMOS, "reason": "explain"})()))
        self.assertEqual(c.shown[-1].view, "book_card")
        asyncio.run(c.on_location(LocationResolved("book", "Cosmos", COSMOS, "Science", 3, "3-SCI-A", 4.0, 1.0)))
        self.assertEqual(c.shown[-1].view, "directions")
        self.assertEqual(c.shown[-1].state_path, "ENGAGED.ANSWER")

    def test_recommendations_are_a_list_with_one_short_reason_each(self):
        c = composer()
        enter(c, "ENGAGED.RECOMMEND")
        picks = c.finder.recommend(COSMOS, 3)
        asyncio.run(c.on_recommendations(RecommendationsReady(
            tuple({"isbn": p.book.isbn13, "because": p.because} for p in picks), COSMOS)))
        update = c.shown[-1]
        self.assertEqual(update.view, "book_list")
        self.assertEqual(update.payload["heading"], "If you like Cosmos")
        self.assertEqual([i["title"] for i in update.payload["items"]], [p.book.title for p in picks])
        for item, pick in zip(update.payload["items"], picks):
            self.assertFalse(item["reason"].lower().startswith("because"))       # "Like Cosmos, it's about ..."
            self.assertTrue(item["reason"][0].isupper())
            self.assertLessEqual(len(item["reason"]), 70)
            self.assertIn(item["reason"].rstrip("…")[1:20], pick.because)
        enter(c, "ENGAGED.ANYTHING_ELSE")                                   # the list stays after the answer
        self.assertEqual(c.shown[-1].view, "book_list")

    def test_ambiguous_words_give_an_options_view(self):
        c = composer()
        enter(c, "ENGAGED.ANSWER")
        asyncio.run(c.on_choices(ChoicesOffered("Which one did you mean?", (
            {"id": "section:History", "label": "The History section"}, {"id": f"where:{SAPIENS}", "label": "Sapiens"}))))
        self.assertEqual(c.shown[-1].view, "options")
        self.assertEqual(c.shown[-1].payload["prompt"], "Which one did you mean?")

    def test_the_options_view_does_not_repeat_the_spoken_sentence_in_a_common_area(self):
        common, quiet = composer("common"), composer("quiet")
        spoken = "I can take back a book, tell you about one, recommend something, or show you where a topic is."
        for c, zone in ((common, "common"), (quiet, "quiet")):
            enter(c, "ENGAGED.OFFER", zone, spoken, self.OFFER_OPTIONS)
            c.title = "How can I help?"
            asyncio.run(c.show())
        self.assertEqual(common.shown[-1].payload["prompt"], "How can I help?")
        self.assertEqual(quiet.shown[-1].payload["prompt"], spoken)

    def test_waiting_for_a_book_shows_buttons_only_in_a_quiet_zone(self):
        common, quiet = composer("common"), composer("quiet")
        for c, zone in ((common, "common"), (quiet, "quiet")):
            enter(c, "ENGAGED.AWAIT_BOOK", zone, "Place the book on my pad", ({"id": "more", "label": "Back"},))
        self.assertEqual((common.shown[-1].view, quiet.shown[-1].view), ("face", "options"))

    def test_storing_shows_the_card_with_the_slot_and_no_buttons(self):
        c = composer()
        asyncio.run(c.on_storing(StoringBook(MARTIAN, "The Martian", 4)))
        update = enter(c, "ENGAGED.STORE")
        self.assertEqual((update.view, update.payload["badge"], update.payload["buttons"]),
                         ("book_card", "Storage slot 4", []))

    def test_returning_shows_the_status_with_the_books_left(self):
        for zone in ("common", "quiet"):
            c = composer(zone)
            asyncio.run(c.on_progress(ReturningProgress("driving", 2, "History", "3-HIS-A", 3)))
            update = enter(c, "RETURNING.NAVIGATING", zone)
            self.assertEqual(update.view, "status")
            self.assertEqual((update.payload["destination"], update.payload["books_left"]), ("the History section", 2))
            self.assertEqual(enter(c, "RETURNING.HOMING", zone).payload["destination"], "home")

    def test_a_new_visit_or_a_new_scan_forgets_the_old_content(self):
        c = composer()
        enter(c, "ENGAGED.ANSWER")
        asyncio.run(c.on_location(LocationResolved("section", "the History section", "", "History", 3, "3-HIS-A")))
        enter(c, "ENGAGED.SCAN")
        self.assertEqual(enter(c, "ENGAGED.OFFER").view, "face")
        asyncio.run(c.on_location(LocationResolved("section", "the History section", "", "History", 3, "3-HIS-A")))
        enter(c, "ENGAGED.FAREWELL")
        self.assertEqual(enter(c, "ENGAGED.GREET").view, "face")
        self.assertIsNone(c.content)


class PayloadTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_every_view_serialises_to_json_and_back(self):
        c = composer()
        for view in VIEWS:
            update = asyncio.run(c.force(view, ""))
            self.assertEqual(update.view, view)
            text = json.dumps(update.payload)
            again = payload_from_json(view, json.loads(text))
            self.assertEqual(again.to_json(), update.payload)
            self.assertEqual(update.layout, "full_face" if view == "face" else "split")

    def test_the_whole_screen_update_is_json(self):
        c = composer()
        update = asyncio.run(c.force("directions", "History"))
        from dataclasses import asdict
        data = json.loads(json.dumps(asdict(update)))
        self.assertEqual((data["view"], data["payload"]["section"]), ("directions", "History"))

    def test_trimming_keeps_whole_words(self):
        self.assertEqual(trim("one two three four", 12), "one two…")
        self.assertEqual(trim("short", 20), "short")
        self.assertEqual(trim("a,  b", 20), "a, b")

    def test_the_summary_is_shorter_in_a_common_area(self):
        quiet, common = composer("quiet"), composer("common")
        quiet.zone, common.zone = "quiet", "common"
        long_one = max(quiet.finder.catalog.books, key=lambda b: len(b.summary or "")).isbn13
        self.assertGreater(len(quiet.book_card(long_one).summary), len(common.book_card(long_one).summary))


class TtlTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_content_goes_back_to_the_face_after_the_zone_ttl(self):
        for zone, ttl in (("common", 15), ("quiet", 30)):
            c = composer(zone)
            enter(c, "IDLE", zone)                                      # outside a visit the time limit applies
            asyncio.run(c.force("directions", "History"))               # (e.g. the operator forced a view)
            self.assertEqual((c.shown[-1].view, c.shown[-1].ttl_s), ("directions", ttl))
            c.clock_.now += ttl - 1
            self.assertFalse(asyncio.run(c.tick()))
            c.clock_.now += 2
            self.assertTrue(asyncio.run(c.tick()))
            self.assertEqual(c.shown[-1].view, "face")

    def test_an_interaction_keeps_the_content_up(self):
        c = composer()
        enter(c, "IDLE")
        asyncio.run(c.force("directions", "History"))
        c.clock_.now += 10
        c.on_interaction(UserUtterance("hmm"))
        c.clock_.now += 10
        self.assertFalse(asyncio.run(c.tick()))
        c.clock_.now += 6
        self.assertTrue(asyncio.run(c.tick()))

    def test_while_a_visitor_is_being_helped_the_map_list_and_card_stay_up(self):
        for zone, ttl in (("common", 15), ("quiet", 30)):
            c = composer(zone)
            enter(c, "ENGAGED.ANSWER", zone)
            asyncio.run(c.on_location(LocationResolved("section", "the History section", "", "History", 3, "3-HIS-A")))
            enter(c, "ENGAGED.ANYTHING_ELSE", zone, "Anything else?", self.OPTS)
            c.clock_.now += ttl * 10
            self.assertFalse(asyncio.run(c.tick()))
            self.assertEqual(c.current.view if c.current else c.shown[-1].view, "directions")
            enter(c, "IDLE", zone)
            self.assertEqual(c.shown[-1].view, "face")

    OPTS = ({"id": "more", "label": "Yes, more help"},)

    def test_a_late_state_event_does_not_wipe_the_content_of_a_newer_state(self):
        c = composer()
        enter(c, "ENGAGED.RECOMMEND")
        picks = c.finder.recommend(COSMOS, 3)
        asyncio.run(c.on_recommendations(RecommendationsReady(
            tuple({"isbn": p.book.isbn13, "because": p.because} for p in picks), COSMOS)))
        asyncio.run(c.on_state(StateEntered("ENGAGED.SCAN", "common", "", "Reading the book...")))    # arrives late (the machine is in RECOMMEND)
        self.assertEqual(c.shown[-1].view, "book_list")
        self.assertIsNotNone(c.content)

    def test_the_app_ticker_returns_to_the_face(self):
        app = make_app()
        app.screen.clock = lambda: app.now[0]
        shown = []
        app.bus.subscribe(ScreenUpdate, shown.append)
        run(app, PersonEngaged(), UserUtterance("where is the history section"))
        self.assertEqual(shown[-1].view, "directions")
        app.now[0] += 20

        async def tick():
            await app.screen.tick()
        asyncio.run(tick())
        self.assertEqual(shown[-1].view, "directions")        # still helping the visitor: the map stays
        run(app, PersonLeft(source="operator"))                # the visit ends: the map goes with it
        self.assertEqual(shown[-1].view, "face")


class DirectionsTest(unittest.TestCase):
    def setUp(self):
        self.plan = FloorPlan.load(PLAN)

    def test_same_floor_draws_an_l_shaped_path_from_the_robot(self):
        p = directions(self.plan, "section", "the History section", 3, (3, 1.0, 6.0), "History")
        self.assertEqual(p.robot, {"x": 1.0, "y": 6.0})
        self.assertEqual(p.start["label"], "You are here")
        centre = self.plan.floor(3).section("History").centre
        self.assertEqual(p.path, [[1.0, 6.0], [centre[0], 6.0], [centre[0], centre[1]]])
        self.assertEqual(p.distance_m, round(abs(centre[0] - 1.0) + abs(centre[1] - 6.0)))
        self.assertFalse(p.other_floor)
        self.assertEqual([s["name"] for s in p.sections if s["highlight"]], ["History"])

    def test_another_floor_starts_with_the_lift(self):
        p = directions(self.plan, "book", "Cosmos", 3, (1, 9.0, 5.5), "Science", "3-SCI-A", (4.0, 1.0))
        self.assertTrue(p.other_floor)
        self.assertEqual(p.steps[0], "Take the lift to Level 3")
        self.assertIsNone(p.robot)
        self.assertEqual(p.start["label"], "Lift")
        self.assertIn("lift", [m["kind"] for m in p.landmarks])
        lift = self.plan.lift(3)
        self.assertEqual(p.path[0], [lift.x, lift.y])
        self.assertEqual(p.path[-1], [4.0, 1.0])
        self.assertEqual(p.buttons, [{"id": "guide", "label": "Guide me"}])

    def test_a_floor_lists_its_sections(self):
        p = directions(self.plan, "floor", "Level 3", 3, (1, 9.0, 5.5))
        self.assertEqual(p.steps[-1], "Level 3: Science, History, Computing")
        self.assertTrue(all(s["highlight"] for s in p.sections))
        self.assertEqual(p.buttons, [])

    def test_l_path_is_straight_when_aligned(self):
        self.assertEqual(l_path((1, 1), (1, 5)), [[1, 1], [1, 5]])
        self.assertEqual(l_path((1, 1), (4, 5)), [[1, 1], [4, 1], [4, 5]])

    def test_an_unknown_floor_gives_nothing(self):
        self.assertIsNone(directions(self.plan, "floor", "Level 9", 9, (1, 0, 0)))

    def test_the_robot_is_at_the_station_unless_returning(self):
        c = composer()
        self.assertEqual(c.robot_position(), (1, 9.0, 5.5))
        c.on_navigation(NavigationRequested("", "shelf 2-SF-A", 1.0, 2.0, 1.57, shelf="2-SF-A", floor=2))
        self.assertEqual(c.robot_position(), (1, 9.0, 5.5))            # with a visitor at the station
        c.path = "RETURNING.NAVIGATING"
        self.assertEqual(c.robot_position(), (2, 1.0, 2.0))             # during a trip: the last target


class FloorPlanTest(unittest.TestCase):
    def test_the_sample_plan_loads_and_matches_the_shelves(self):
        plan = FloorPlan.load(PLAN)
        self.assertEqual(list(plan.floors), [1, 2, 3])
        self.assertEqual(plan.where_section("science fiction")[0].level, 2)
        self.assertEqual(plan.station.level, 1)
        for level in plan.floors:
            self.assertIsNotNone(plan.lift(level))

    def bad(self, data):
        with self.assertRaises(FloorPlanError) as caught:
            FloorPlan.from_dict(data, "plan.yaml")
        return " | ".join(caught.exception.problems)

    def floor(self, **kw):
        return {"level": 1, "width": 10, "height": 8, "sections": [], **kw}

    def test_problems_are_all_reported_with_where_they_are(self):
        self.assertIn("needs a `floors:` list", self.bad({}))
        self.assertIn("is defined twice", self.bad({"floors": [self.floor(), self.floor()]}))
        self.assertIn("outside the floor", self.bad({"floors": [self.floor(sections=[{"name": "A", "x": 8, "y": 0, "w": 5, "h": 1}])]}))
        self.assertIn("`w` must be more than 0", self.bad({"floors": [self.floor(sections=[{"name": "A", "x": 0, "y": 0, "w": 0, "h": 1}])]}))
        self.assertIn("kind 'tower'", self.bad({"floors": [self.floor(landmarks=[{"kind": "tower", "x": 1, "y": 1}])]}))
        self.assertIn("must be unique", self.bad({"floors": [self.floor(sections=[{"name": "A", "x": 0, "y": 0, "w": 1, "h": 1}]),
                                                            {**self.floor(sections=[{"name": "a", "x": 0, "y": 0, "w": 1, "h": 1}]), "level": 2}]}))
        self.assertIn("station: level 5", self.bad({"floors": [self.floor()], "station": {"level": 5, "x": 1, "y": 1}}))
        both = self.bad({"floors": [self.floor(width=-1), self.floor(level=2, sections=[{"x": 1}])]})
        self.assertIn("`width` must be more than 0", both)
        self.assertIn("needs a name", both)

    def test_unreadable_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.yaml"
            path.write_text("floors: [")
            with self.assertRaises(FloorPlanError):
                FloorPlan.load(path)
            with self.assertRaises(FloorPlanError):
                FloorPlan.load(Path(d) / "missing.yaml")


def resolver():
    app = make_app()
    return PlaceResolver(app.finder, FloorPlan.load(PLAN))


class PlaceResolutionTest(unittest.TestCase):
    def test_sections_topics_titles_and_floors(self):
        r = resolver()
        cases = {"where are the science books": ("section", "Science"), "take me to history": ("section", "History"),
                 "science fiction": ("section", "Science Fiction"), "where are the astronomy books": ("section", "Science"),
                 "where is space stuff": ("section", "Science")}
        for text, (kind, section) in cases.items():
            place = r.resolve(text)
            self.assertEqual((place.kind, place.section), (kind, section), text)
        self.assertEqual(r.resolve("where are the astronomy books").topic, "astronomy")
        dune = r.resolve("where is Dune")
        self.assertEqual((dune.kind, dune.title, dune.floor, dune.shelf), ("book", "Dune", 2, "2-SF-B"))
        self.assertEqual(r.resolve("where is frankenstien").title, "Frankenstein")          # a typo
        self.assertEqual(r.resolve("where is foundaton").title, "Foundation")
        for text, level in (("what's on level 2", 2), ("floor 3", 3), ("the ground floor", 1)):
            place = r.resolve(text)
            self.assertEqual((place.kind, place.floor), ("floor", level), text)
        self.assertIsNone(r.resolve("what's on level 9"))
        self.assertIsNone(r.resolve("where is the bathroom"))

    def test_a_section_name_inside_a_title_is_ambiguous_only_when_more_was_said(self):
        r = resolver()
        place = r.resolve("where is the brief history")
        self.assertEqual(place.kind, "ambiguous")
        self.assertEqual({c.kind for c in place.choices}, {"book", "section"})
        self.assertEqual(r.resolve("take me to history").kind, "section")

    def test_equally_good_titles_or_topics_are_ambiguous(self):
        catalog = Catalog([Book("9780000000002", "The Red Room", shelf="A1"), Book("9780000000019", "The Red Rose", shelf="B1"),
                           Book("9780000000026", "Stars", subjects=("astronomy",), shelf="A1"),
                           Book("9780000000033", "Planets", subjects=("astronomy",), shelf="B1")])
        shelves = {"A1": Shelf("A1", "East", 1, "common", 1, 1, 0), "B1": Shelf("B1", "West", 1, "common", 5, 1, 0)}
        vocab = TopicVocabulary({"astronomy": {"aliases": ["space"]}})
        finder = BookFinder(catalog, online_fallback=False, graph=BookGraph(catalog, vocab, shelves))
        r = PlaceResolver(finder)
        titles = r.resolve("where is the red ro")
        self.assertEqual(titles.kind, "ambiguous")
        self.assertEqual(sorted(c.title for c in titles.choices), ["The Red Room", "The Red Rose"])
        topic = r.resolve("where are the space books")
        self.assertEqual((topic.kind, sorted(c.section for c in topic.choices)), ("ambiguous", ["East", "West"]))
        self.assertEqual(topic.choices[0].choice_id, "section:East")


class DialogueViewsTest(unittest.TestCase):
    """The requests that lead to views, end to end through the app."""

    def setUp(self):
        quiet_logs(self)

    def app(self, zone="common"):
        from robot.events import ContextChanged, ReplySentence
        app = make_app()
        app.shown, app.said, app.nav = [], [], []
        app.bus.subscribe(ScreenUpdate, app.shown.append)
        app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
        app.bus.subscribe(NavigationRequested, app.nav.append)
        run(app, ContextChanged(zone, 1), PersonEngaged())
        return app

    def views(self, app):
        return [u.view for u in app.shown]

    def test_engage_ask_book_where_guide_science_recommend_second(self):
        for zone in ("common", "quiet"):
            app = self.app(zone)
            run(app, UserChoice("ask"), BookPlaced(), scanned(COSMOS))
            self.assertEqual(app.shown[-1].view, "book_card")
            run(app, UserUtterance("where is it"))
            self.assertEqual(app.shown[-1].view, "directions")
            self.assertEqual(app.shown[-1].payload["steps"][0], "Take the lift to Level 3")
            run(app, UserUtterance("guide me"))
            self.assertEqual(app.nav[-1].shelf, "3-SCI-A")
            run(app, UserUtterance("where are the science books"))
            self.assertEqual((app.shown[-1].view, app.shown[-1].payload["section"]), ("directions", "Science"))
            run(app, UserUtterance("recommend something similar"))
            self.assertEqual(app.shown[-1].view, "book_list")
            items = app.shown[-1].payload["items"]
            run(app, UserUtterance("the second one" if len(items) > 1 else "the first one"))
            chosen = items[1 if len(items) > 1 else 0]["title"]
            self.assertEqual((app.shown[-1].view, app.shown[-1].payload["title"]), ("book_card", chosen))

    def test_whats_on_a_floor(self):
        app = self.app()
        run(app, UserUtterance("what's on level 2?"))
        self.assertIn("Level 2 has the Science Fiction section", " ".join(app.said))
        self.assertEqual((app.shown[-1].view, app.shown[-1].payload["kind"]), ("directions", "floor"))

    def test_ambiguous_words_ask_which_and_a_tap_or_an_ordinal_answers(self):
        app = self.app("quiet")
        run(app, UserUtterance("where is the brief history"))       # the where-flow finds the book first
        self.assertEqual(app.shown[-1].payload.get("title"), "A Brief History of Time")
        app.dialogue.find_book = lambda q: None                      # force the resolver's ambiguous answer
        run(app, UserUtterance("where is the brief history"))
        self.assertEqual(app.shown[-1].view, "options")
        self.assertIn("Did you mean", app.said[-1])
        ids = [o["id"] for o in app.shown[-1].payload["options"]]
        self.assertIn("section:History", ids)
        run(app, UserChoice("section:History", source="screen"))
        self.assertEqual((app.shown[-1].view, app.shown[-1].payload["section"]), ("directions", "History"))

    def test_card_buttons_and_list_taps(self):
        app = self.app()
        run(app, UserUtterance("recommend something like dune"))
        first = app.shown[-1].payload["items"][0]["isbn"]
        run(app, UserChoice(f"book:{first}", source="screen"))
        self.assertEqual((app.shown[-1].view, app.shown[-1].payload["isbn"]), ("book_card", first))
        run(app, UserChoice(f"similar:{first}", source="screen"))
        self.assertEqual(app.shown[-1].view, "book_list")
        self.assertIn(app.finder.catalog.lookup(first).title, app.shown[-1].payload["heading"])
        run(app, UserChoice(f"guide:{first}", source="screen"))
        self.assertEqual(app.nav[-1].isbn, first)

    def test_the_state_machine_has_no_new_states(self):
        from robot.core.dialogue import SPEC
        self.assertEqual(len(SPEC.states), 17)


class CoversTest(unittest.TestCase):
    def test_the_placeholder_has_the_title_on_a_colour_from_the_isbn(self):
        svg = placeholder_svg(COSMOS, "Cosmos & <Friends>", "Carl Sagan")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn(">Cosmos &amp;</tspan>", svg)                         # escaped, wrapped
        self.assertIn(">&lt;Friends&gt;</tspan>", svg)
        self.assertIn(cover_colour(COSMOS), svg)
        self.assertEqual(cover_colour(COSMOS), cover_colour(COSMOS))
        self.assertNotEqual(cover_colour(COSMOS), cover_colour(MARTIAN))
        long_title = placeholder_svg("1", "A very long title that goes on and on and on and on and on forever more", "")
        self.assertLessEqual(long_title.count("<tspan"), 5)

    def test_download_saves_a_jpeg_and_reports_404_as_missing(self):
        jpeg = b"\xff\xd8" + b"x" * 2000

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as d:
            seen = []

            def opener(request, timeout):
                seen.append(request.full_url)
                return Response(jpeg)
            self.assertEqual(download_cover(COSMOS, d, opener=opener), "saved")
            self.assertIn(f"/b/isbn/{COSMOS}-L.jpg?default=false", seen[0])
            self.assertEqual(cover_file(d, COSMOS).read_bytes(), jpeg)
            self.assertEqual(download_cover(COSMOS, d, opener=opener), "exists")

            def missing(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            self.assertEqual(download_cover(MARTIAN, d, opener=missing), "missing")
            self.assertIsNone(cover_file(d, MARTIAN))
            self.assertEqual(download_cover(HITCH, d, opener=lambda r, timeout: Response(b"tiny")), "missing")

            def limited(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            self.assertIn("rate limit", download_cover(HITCH, d, opener=limited))

    def test_import_books_covers_only_downloads_with_a_polite_delay(self):
        from tools import import_books as ib
        jpeg = b"\xff\xd8" + b"y" * 2000

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{COSMOS}.jpg").write_bytes(jpeg)                    # already there: not asked for
            asked, slept = [], []

            def opener(request, timeout):
                asked.append(request.full_url)
                if MARTIAN in request.full_url:
                    raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
                return Response(jpeg)
            with redirect_stdout(io.StringIO()):
                result = ib.fetch_covers([COSMOS, HITCH, MARTIAN], Path(d), 3.0, opener=opener, sleep=slept.append)
            self.assertEqual((result["saved"], result["exists"], result["missing"]), ([HITCH], [COSMOS], [MARTIAN]))
            self.assertEqual(len(asked), 2)
            self.assertEqual(slept, [3.0])                                    # between the two requests only

    def test_import_books_covers_only_command_line_uses_the_catalogue(self):
        from tools import import_books as ib
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ib, "fetch_covers", return_value={
                "saved": [], "exists": [], "missing": [], "failed": []}) as fetch, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ib.main(["--covers-only", "--covers-dir", d]), 0)
        isbns = fetch.call_args[0][0]
        self.assertIn(COSMOS, isbns)
        self.assertGreaterEqual(fetch.call_args[0][2], 3.0)
        self.assertIn("Covers for", out.getvalue())


class CheckerTest(unittest.TestCase):
    def test_the_sample_data_passes_with_covers_only_warned(self):
        from tools.catalog_check import check
        with tempfile.TemporaryDirectory() as covers:
            report = check(ROOT / "data/catalog.csv", ROOT / "data/topics.yaml", ROOT / "data/shelves.csv",
                           ROOT / "data/curated_links.csv", floorplan_path=PLAN, covers_dir=covers)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("have no cover" in w for w in report.warnings))
        self.assertTrue(any("floorplan.yaml (3 floors)" in s for s in report.summary))

    def test_a_shelf_off_the_plan_or_outside_its_section_is_an_error(self):
        from tools.catalog_check import check
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shelves = d / "shelves.csv"
            shelves.write_text("shelf,section,floor,zone,map_x,map_y,map_yaw,description\n"
                               "3-SCI-A,Science,3,quiet,9.5,7.5,3.14,outside\n"
                               "2-SF-A,Science Fiction,3,common,1.0,2.0,1.57,wrong floor\n"
                               "9-NEW-A,Poetry,1,common,1,1,0,unknown section\n")
            catalog = d / "catalog.csv"
            catalog.write_text("isbn13,title,author,genre,subjects,summary,shelf,slot,call_number,section,floor,series,series_index\n")
            report = check(catalog, None, shelves, None, floorplan_path=PLAN)
            text = " | ".join(report.errors)
            self.assertIn("shelf 3-SCI-A at (9.5, 7.5) is outside its section 'Science'", text)
            self.assertIn("shelf 2-SF-A is in section 'Science Fiction' on level 3, but the plan has that section on level 2", text)
            self.assertIn("shelf 9-NEW-A is in section 'Poetry' on level 1, but the plan has no such section", text)
            bad = d / "plan.yaml"
            bad.write_text("floors: []")
            report = check(catalog, None, shelves, None, floorplan_path=bad)
            self.assertTrue(any("plan.yaml" in e for e in report.errors))


class ServerAndOperatorTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.covers = tempfile.TemporaryDirectory()
        self.addCleanup(self.covers.cleanup)
        self.hub = Hub(covers_dir=Path(self.covers.name))
        self.server = make_server(self.hub, port=0, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=5)

    def test_covers_are_served_or_made_up(self):
        with self.get(f"/covers/{COSMOS}?title=Cosmos&author=Carl+Sagan") as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"Cosmos", r.read())
        (Path(self.covers.name) / f"{COSMOS}.jpg").write_bytes(b"\xff\xd8jpeg")
        with self.get(f"/covers/{COSMOS}") as r:
            self.assertEqual((r.headers["Content-Type"], r.read()), ("image/jpeg", b"\xff\xd8jpeg"))
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/covers/../../etc/passwd")
        self.assertTrue(allowed("GET", f"/covers/{COSMOS}", False))

    def test_content_choices_are_accepted_and_junk_is_not(self):
        for choice in (f"book:{COSMOS}", f"similar:{COSMOS}", f"guide:{COSMOS}", f"where:{COSMOS}",
                       "section:Science Fiction", "floor:2"):
            self.post("/input", {"choice": choice}).close()
        for bad in ("book:abc", "section:<script>", "floor:x", "rm:-rf"):
            with self.assertRaises(urllib.error.HTTPError):
                self.post("/input", {"choice": bad})
        self.post("/operator", {"command": "screen", "arg": "directions History"}).close()

    def test_the_page_loads_the_views_and_has_every_view(self):
        with self.get("/") as r:
            page = r.read().decode()
        self.assertIn('<script src="views.js"></script>', page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))      # offline: nothing from the internet
        with self.get("/views.js") as r:
            views = r.read().decode()
        for fn in ("function bookCard", "function bookList", "function directionsView", "function options",
                   "function status", "function face", "/input"):
            self.assertIn(fn, views.replace("fetch(\"/input\"", "/input"))
        self.assertNotIn("innerHTML", views)

    def test_the_operator_screen_command_forces_any_view(self):
        app = make_app()
        shown = []
        app.bus.subscribe(ScreenUpdate, shown.append)
        said = []
        op = Operator(app, said.append)
        for view, arg in (("book_card", COSMOS), ("book_list", COSMOS), ("directions", "History"),
                          ("directions", COSMOS), ("options", ""), ("status", ""), ("face", "")):
            self.assertTrue(asyncio.run(op.run("screen", f"{view} {arg}".strip())), view)
            self.assertEqual(shown[-1].view, view)
        self.assertEqual(shown[-4].payload["title"], "Cosmos")
        self.assertFalse(asyncio.run(op.run("screen", "poster")))
        self.assertIn("no view called", said[-1])
        self.assertIn("view", app.state_info())


if __name__ == "__main__":
    unittest.main()
