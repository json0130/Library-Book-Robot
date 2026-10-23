"""The robot's state machine: checks on the transition table, and whole visits in both zones."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from robot.config import RETURNING_DEFAULTS, STORAGE_DEFAULTS, TIMEOUTS_DEFAULTS
from robot.core.dialogue import SPEC, TIMER_SETTINGS
from robot.core.state_doc import DOC_PATH, render_markdown
from robot.core.storage import StoredBook
from robot.events import (BookDropped, BookPlaced, ContextChanged, NavigationRequested, PersonEngaged, PersonLeft,
                          PersonNearbyBusy, ReplySentence, ScanFailed, ScreenUpdate, StateTransition, UserChoice,
                          UserUtterance)
from robot.operator import Operator
from tests.test_dialogue import HITCH, MARTIAN, make_app, advance, run, scanned

ZONES = ("common", "quiet")


def quiet_logs(test):
    logging.disable(logging.INFO)
    test.addCleanup(logging.disable, logging.NOTSET)


def watch(app):
    """Record the events a test wants to look at: what was said, the navigation requests, the transitions, the screens."""
    app.said, app.nav, app.moves, app.screens = [], [], [], []
    app.bus.subscribe(ReplySentence, lambda e: app.said.append(e.text))
    app.bus.subscribe(NavigationRequested, app.nav.append)
    app.bus.subscribe(StateTransition, app.moves.append)
    app.bus.subscribe(ScreenUpdate, app.screens.append)
    return app


def new_app(zone="common", **kw):
    app = watch(make_app(**kw))
    app.dialogue.settings.returning["auto_start"] = True       # these tests cover the trip; config/robot.yaml has it off
    run(app, ContextChanged(zone, 1))
    return app


def sent(app, command):
    return [params for cmd, params in app.actuators.lights.esp32.sent if cmd == command]


def go(app, state):
    return asyncio.run(app.dialogue.goto(state))


def stored_app(zone="common", books=2):
    """An app in IDLE with `books` returned books waiting in storage, on different shelves."""
    app = new_app(zone)
    app.dialogue.fill_storage(books)
    assert len(app.dialogue.storage) == books
    return app


class TableTest(unittest.TestCase):
    def test_every_state_can_be_reached_from_the_start(self):
        self.assertEqual(SPEC.reachable(), set(SPEC.leaves()))

    def test_every_waiting_state_has_a_timeout_and_a_way_out_when_the_person_leaves(self):
        waiting = [s.name for s in SPEC.states.values() if s.waiting]
        self.assertGreaterEqual(len(waiting), 6)
        for name in waiting:
            events = {t.event for t in SPEC.effective_transitions(name)}
            self.assertIn("PersonLeft", events, name)
            self.assertIn("timeout", events, name)
            timers = [t for state in (name, *SPEC.ancestors(name)) for t in SPEC.states[state].timers]
            self.assertIn("timeout", {t.event for t in timers}, f"{name} never times out")
            for t in SPEC.effective_transitions(name):
                if t.event in ("PersonLeft", "timeout") and t.target is not None:       # (not the "keep waiting" one)
                    self.assertEqual(SPEC.leaf_of(t.target), "FAREWELL", (name, t))

    def test_no_state_is_a_dead_end(self):
        for name in SPEC.leaves():
            outgoing = [t for t in SPEC.effective_transitions(name) if t.target is not None]
            self.assertTrue(outgoing, f"{name} has no way out")

    def test_every_session_state_leads_to_the_end_of_the_session(self):
        """Following the table from any ENGAGED state reaches FAREWELL, and FAREWELL reaches IDLE."""
        for name in SPEC.leaves():
            if "ENGAGED" not in SPEC.ancestors(name):
                continue
            seen, todo = {name}, [name]
            while todo:
                for t in SPEC.effective_transitions(todo.pop()):
                    for target in SPEC.targets_of(t):
                        if target not in seen:
                            seen.add(target)
                            todo.append(target)
            self.assertIn("FAREWELL", seen, name)
        self.assertIn("IDLE", {SPEC.leaf_of(t.target) for t in SPEC.effective_transitions("FAREWELL")
                              if t.target is not None and not hasattr(t.target, "state")})

    def test_the_three_top_level_states_and_the_nine_engaged_ones_exist(self):
        self.assertEqual(SPEC.children("ENGAGED"),
                         ["GREET", "OFFER", "AWAIT_BOOK", "SCAN", "STORE", "ANSWER", "RECOMMEND", "ANYTHING_ELSE",
                          "FAREWELL"])
        self.assertEqual([n for n in SPEC.states if SPEC.parent(n) is None], ["IDLE", "ENGAGED", "RETURNING"])
        self.assertEqual(set(SPEC.children("RETURNING")), {"PLAN", "NAVIGATING", "AT_SHELF", "ANNOUNCE", "HOMING"})

    def test_the_zone_and_the_fixed_mode_are_not_states(self):
        for name in SPEC.states:
            self.assertNotIn(name.lower(), ("quiet", "common", "fixed"))

    def test_every_timer_is_a_documented_config_key(self):
        defaults = {"timeouts": TIMEOUTS_DEFAULTS, "returning": RETURNING_DEFAULTS, "storage": STORAGE_DEFAULTS}
        for state in SPEC.states.values():
            for timer in state.timers:
                if timer.seconds == "t_farewell":
                    continue
                section, key = TIMER_SETTINGS[timer.seconds]
                self.assertIn(key, defaults[section], f"{state.name}: {section}.{key}")

    def test_the_handler_has_every_method_the_table_names(self):
        make_app()                                   # the Machine checks this when the Dialogue is built
        self.assertIn("commit_store", SPEC.method_names())

    def test_the_generated_document_is_up_to_date(self):
        self.assertTrue(DOC_PATH.is_file(), "run: python -m robot states --write")
        self.assertEqual(DOC_PATH.read_text(encoding="utf-8"), render_markdown(),
                         "docs/state_machine.md is out of date: run `python -m robot states --write`")

    def test_the_mermaid_diagram_covers_the_states(self):
        text = SPEC.mermaid(grouped=True)
        self.assertTrue(text.startswith("stateDiagram-v2"))
        for name in SPEC.states:
            self.assertIn(name, text)
        self.assertIn("state ENGAGED {", text)
        self.assertIn("(history)", text)
        self.assertIn("(previous)", text)


class ReturnFlowTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_a_full_return_then_the_automatic_trip_back_to_the_shelf_in_both_zones(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged(), UserChoice("return", source="screen"))
                self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
                run(app, BookPlaced(), scanned(MARTIAN))
                self.assertEqual(app.dialogue.state, "ENGAGED.STORE")
                self.assertEqual(app.dialogue.storage.slot_of(MARTIAN), None)       # promised, not stored yet
                run(app, BookDropped(source="operator"))
                self.assertEqual(app.dialogue.storage.slot_of(MARTIAN), 4)
                self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                run(app, UserUtterance("no thanks"))
                self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
                advance(app, 5)
                self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")              # waiting the auto-start delay
                advance(app, 10)
                self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
                (shelf,) = app.nav
                self.assertEqual((shelf.shelf, shelf.section), ("2-SF-C", "Science Fiction"))
                advance(app, 6)
                self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")
                advance(app, 3)
                self.assertEqual(app.dialogue.state, "RETURNING.HOMING")
                self.assertTrue(app.dialogue.storage.empty)                              # the slot was freed at the shelf
                advance(app, 6)
                self.assertEqual(app.dialogue.state, "IDLE")
                self.assertEqual([e.title for e in app.nav], ["shelf 2-SF-C", "home"])
                states = [m.to_path for m in app.moves if not m.internal]
                for expected in ("ENGAGED.GREET", "ENGAGED.AWAIT_BOOK", "ENGAGED.SCAN", "ENGAGED.STORE",
                                 "ENGAGED.FAREWELL", "RETURNING.PLAN", "RETURNING.NAVIGATING", "RETURNING.AT_SHELF",
                                 "RETURNING.HOMING", "IDLE"):
                    self.assertIn(expected, states)

    def test_with_auto_start_off_the_robot_stays_idle_and_keeps_the_book(self):
        app = new_app()
        app.dialogue.settings.returning["auto_start"] = False
        run(app, PersonEngaged(), UserUtterance("return"), BookPlaced(), scanned(MARTIAN), BookDropped(), UserUtterance("bye"))
        advance(app, 30)
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertEqual(len(app.dialogue.storage), 1)

    def test_a_person_engaging_during_the_goodbye_cancels_the_automatic_start(self):
        app = stored_app()
        run(app, PersonEngaged(), UserUtterance("bye"))
        advance(app, 5)
        run(app, PersonEngaged())
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
        advance(app, 20)                                                             # waiting on the offer, no trip started
        self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")

    def test_several_shelves_are_visited_in_order_and_each_unload_frees_its_slots(self):
        app = stored_app(books=3)
        shelves = [s.shelf for s in app.dialogue.storage.stops()]
        self.assertEqual(len(shelves), 3)
        go(app, "PLAN")
        visited = []
        for _ in range(3):
            self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
            visited.append(app.nav[-1].shelf)
            advance(app, 6)
            advance(app, 3)
        self.assertEqual(visited, shelves)
        self.assertEqual(app.dialogue.state, "RETURNING.HOMING")
        self.assertTrue(app.dialogue.storage.empty)


class SessionFlowTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_ask_about_a_book_without_placing_one(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged(), UserUtterance("where is dune?"))
                self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                self.assertIn("Dune is on shelf 2-SF-B", " ".join(app.said))
                self.assertIsNone(app.dialogue.book)

    def test_choosing_ask_with_no_book_waits_for_one(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("ask"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        run(app, BookPlaced(), scanned(HITCH))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANSWER")                       # it asks what they want to know
        self.assertIn("What would you like to know?", app.said[-1])
        run(app, UserUtterance("explain"))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertIn("whisked off Earth", " ".join(app.said))

    def test_recommend_by_a_spoken_interest_needs_no_book(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged(), UserUtterance("can you recommend something about space?"))
                self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                text = " ".join(app.said)
                self.assertIn("If you're interested in space", text)
                self.assertNotIn("Place", text)

    def test_choosing_recommend_then_saying_an_interest(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("recommend"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        self.assertIn("What kind of books do you like?", app.said[-1])
        run(app, UserUtterance("dystopian novels"))                                  # a bare topic is the answer
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertIn("Nineteen Eighty-Four", " ".join(app.said))

    def test_a_book_placed_in_any_engaged_state_goes_to_scan_and_then_follows_the_intent(self):
        for start in ("OFFER", "AWAIT_BOOK", "ANSWER", "ANYTHING_ELSE"):
            with self.subTest(start=start):
                app = new_app()
                go(app, start)
                app.dialogue.session.book = None
                run(app, BookPlaced())
                self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")
                run(app, scanned(MARTIAN))
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")                # no intent yet: offer with the title
                self.assertIn("You have The Martian", app.said[-1])

    def test_the_book_follows_the_chosen_intent(self):
        for intent, final in (("recommend", "ENGAGED.ANYTHING_ELSE"), ("ask", "ENGAGED.ANSWER"),
                              ("return", "ENGAGED.STORE")):
            with self.subTest(intent=intent):
                app = new_app()
                run(app, PersonEngaged(), UserChoice(intent), BookPlaced(), scanned(MARTIAN))
                self.assertEqual(app.dialogue.state, final)

    def test_a_failed_scan_asks_again_and_the_second_failure_goes_back_to_the_offer(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), ScanFailed("a"))
                self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
                self.assertIn("barcode facing up", app.said[-1])
                run(app, BookPlaced(), ScanFailed("b"))
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                self.assertIn("still can't read that barcode", app.said[-1])
                run(app, BookPlaced(), scanned(MARTIAN))                              # and a third try can succeed
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                self.assertEqual(app.dialogue.session.scan_failures, 0)

    def test_a_full_storage_ring_says_so_and_asks_if_there_is_anything_else(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                storage = app.dialogue.storage
                for slot in range(storage.size):
                    storage.commit(slot, StoredBook(isbn=f"x{slot}", title=f"Book {slot}"))
                self.assertTrue(storage.full)
                run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN))
                self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                self.assertIn("storage is full", " ".join(app.said))
                self.assertNotIn("slot_light", [c for c, _ in app.actuators.lights.esp32.sent])
                self.assertEqual(len(storage), storage.size)

    def test_the_slot_is_given_back_if_the_book_is_never_dropped(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN))
        self.assertEqual(app.dialogue.state, "ENGAGED.STORE")
        self.assertNotIn(4, app.dialogue.storage.free_slots())                          # promised
        run(app, PersonLeft(source="operator"))
        self.assertIn(4, app.dialogue.storage.free_slots())                             # given back
        self.assertEqual(sent(app, "slot_light")[-1], {"slot": 4, "on": False})
        self.assertTrue(app.dialogue.storage.empty)

    def test_the_simulated_drop_fires_after_store_sim_drop_s(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN))
        advance(app, 5)                                                                 # sim_drop_s is 4
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertEqual(app.dialogue.storage.slot_of(MARTIAN), 4)
        self.assertEqual(app.moves[-1].source, "timeout")

    def test_the_person_leaving_ends_the_visit_from_every_waiting_state(self):
        for state in [s.name for s in SPEC.states.values() if s.waiting]:
            with self.subTest(state=state):
                app = new_app()
                go(app, state)
                run(app, PersonLeft())
                self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
                advance(app, 5)
                self.assertEqual(app.dialogue.state, "IDLE")

    def test_every_waiting_state_times_out_into_a_goodbye(self):
        for state in [s.name for s in SPEC.states.values() if s.waiting]:
            with self.subTest(state=state):
                app = new_app()
                app.dialogue.settings.storage["sim_drop_s"] = 0                          # STORE: wait for the real drop
                app.dialogue.settings.timeouts["enabled"] = True
                go(app, state)
                advance(app, 40)
                self.assertEqual(app.dialogue.state, "IDLE", state)
                self.assertIn("timeout", [m.event for m in app.moves])

    def test_a_quiet_visitor_who_is_still_there_is_not_said_goodbye_to(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = new_app(zone)
                run(app, PersonEngaged())
                said = len(app.said)
                advance(app, 200)                                                       # far past offer_s (30)
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                self.assertEqual(len(app.said), said)                                   # nothing repeated or said
                self.assertNotIn("FAREWELL", " ".join(m.to_path for m in app.moves))
                run(app, PersonLeft())                                                  # but when the camera says they went
                self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")

    def test_each_waiting_state_keeps_waiting_while_the_person_is_there(self):
        for state in [s.name for s in SPEC.states.values() if s.waiting]:
            with self.subTest(state=state):
                app = new_app()
                app.dialogue.settings.storage["sim_drop_s"] = 0
                go(app, state)
                app.dialogue.present = True
                advance(app, 100)
                self.assertNotEqual(app.dialogue.state, "IDLE", state)
                self.assertNotIn("ENGAGED.FAREWELL", [m.to_path for m in app.moves], state)

    def test_a_goodbye_is_spoken_in_a_common_zone_and_silent_in_a_quiet_one(self):
        common, quiet = new_app("common"), new_app("quiet")
        for app in (common, quiet):
            run(app, PersonEngaged(), PersonLeft())
        self.assertIn("Enjoy your reading", common.said[-1])
        self.assertFalse(any("Enjoy" in s for s in quiet.said))                         # not even printed or said
        self.assertFalse(any("Enjoy" in p["text"] for p in sent(quiet, "print")))
        self.assertIn({"name": "pulse"}, sent(quiet, "light_pattern"))                  # eyes and light instead
        self.assertEqual(quiet.screens[-1].state_path, "ENGAGED.FAREWELL")
        self.assertEqual(quiet.screens[-1].text, "Goodbye")

    def test_nothing_about_a_visitor_goes_into_the_graph_or_the_storage_file(self):
        app = new_app()
        graph_before = (len(app.finder.graph.catalog), dict(app.finder.graph.shelves))
        with tempfile.TemporaryDirectory() as d:
            app.dialogue.storage.path = Path(d) / "storage.json"
            run(app, PersonEngaged((1,)), UserUtterance("I like space"), UserChoice("return"), BookPlaced(), scanned(MARTIAN),
                BookDropped())
            data = json.loads(app.dialogue.storage.path.read_text())
        self.assertEqual((len(app.finder.graph.catalog), dict(app.finder.graph.shelves)), graph_before)
        self.assertNotIn("person", json.dumps(data).lower())
        self.assertNotIn("space", json.dumps(data).lower())


class ScreenTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_every_state_entry_publishes_a_screen_update_with_path_zone_and_layout(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        paths = [u.state_path for u in app.screens]
        for path in ("IDLE", "ENGAGED.GREET", "ENGAGED.OFFER", "ENGAGED.AWAIT_BOOK", "ENGAGED.SCAN", "ENGAGED.STORE",
                     "ENGAGED.ANYTHING_ELSE"):
            self.assertIn(path, paths)
        for update in app.screens:
            self.assertEqual(update.zone, "quiet")
            self.assertIn(update.layout, ("full_face", "split"))
        store = next(u for u in app.screens if u.state_path == "ENGAGED.STORE")
        self.assertIn("storage slot 4", store.text)

    def test_buttons_are_offered_in_a_quiet_zone_only(self):
        quiet, common = new_app("quiet"), new_app("common")
        for app in (quiet, common):
            run(app, PersonEngaged())
        offer = next(u for u in reversed(quiet.screens) if u.state_path == "ENGAGED.OFFER")
        self.assertEqual([o["id"] for o in offer.options], ["return", "ask", "recommend"])
        self.assertTrue(all(o["label"] for o in offer.options))
        self.assertEqual(next(u for u in reversed(common.screens) if u.state_path == "ENGAGED.OFFER").options, ())

    def test_anything_else_has_buttons_in_a_quiet_zone_and_a_guide_button_when_it_can_drive(self):
        app = new_app("quiet")
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        buttons = [o["id"] for o in app.screens[-1].options]
        self.assertEqual(app.screens[-1].state_path, "ENGAGED.ANYTHING_ELSE")
        self.assertEqual(buttons, ["more", "guide", "done"])

    def test_a_button_tap_is_a_choice_from_the_screen_and_is_logged_as_such(self):
        app = new_app("quiet")
        run(app, PersonEngaged())
        run(app, UserChoice("recommend", source="screen"))
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        move = next(m for m in reversed(app.moves) if m.to_path == "ENGAGED.AWAIT_BOOK")
        self.assertEqual((move.event, move.source, move.zone), ("Choice", "screen", "quiet"))
        run(app, UserChoice("done", source="screen"))
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")

    def test_every_transition_is_logged_with_time_from_to_event_source_and_zone(self):
        with tempfile.TemporaryDirectory() as d:
            app = new_app("quiet", log_dir=Path(d))
            run(app, PersonEngaged(source="operator"), UserChoice("done"))
            lines = [json.loads(l) for l in (Path(d) / "session-test.jsonl").read_text().splitlines()]
        moves = [l for l in lines if l["event"] == "StateTransition"]
        self.assertGreaterEqual(len(moves), 3)
        first = next(m for m in moves if m["data"]["event"] == "PersonEngaged")
        self.assertEqual((first["data"]["from_path"], first["data"]["to_path"], first["data"]["source"], first["data"]["zone"]),
                         ("IDLE", "ENGAGED.GREET", "operator", "quiet"))
        self.assertIsInstance(first["t"], float)
        self.assertEqual(moves[-1]["data"]["to_path"], "ENGAGED.FAREWELL")

    def test_the_zone_changing_mid_visit_changes_the_style_not_the_state(self):
        app = new_app("common")
        run(app, PersonEngaged())
        before = app.dialogue.state
        asyncio.run(Operator(app).run("zone", "quiet"))
        self.assertEqual(app.dialogue.state, before)
        self.assertEqual(app.context.zone, "quiet")
        self.assertTrue(app.style.touch_buttons)


class ReturningInterruptionTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def start_trip(self, zone):
        app = stored_app(zone)
        go(app, "PLAN")
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        return app

    def test_a_person_engaging_pauses_the_trip_and_it_resumes_at_the_same_shelf(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = self.start_trip(zone)
                shelf = app.nav[-1].shelf
                advance(app, 2)
                run(app, PersonEngaged())
                self.assertEqual(app.dialogue.state, "ENGAGED.OFFER")
                self.assertEqual(sent(app, "stop"), [{}])                                # the base was told to stop
                opening = "Hi! I'm returning some books, can I help you with anything?"
                if zone == "common":
                    self.assertIn(opening, app.said)                                     # spoken
                else:
                    self.assertNotIn(opening, app.said)                                  # not a word
                    self.assertTrue(any(u.text.startswith(opening) for u in app.screens))   # but on the screen
                    self.assertIn({"target": "visitor"}, sent(app, "gaze"))              # looking at them
                    self.assertIn({"name": "pulse"}, sent(app, "light_pattern"))         # and pulsing the light
                run(app, UserChoice("done"))
                self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
                advance(app, 5)                                                          # a resume has no auto-start delay
                self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
                self.assertEqual(app.nav[-1].shelf, shelf)                               # the same shelf
                self.assertLess(app.dialogue.fsm.time_left("NAVIGATING", "arrived"), 6)  # with the drive time that was left
                advance(app, 9)
                self.assertIn(app.dialogue.state, ("RETURNING.AT_SHELF", "RETURNING.NAVIGATING"))

    def test_the_remaining_drive_time_is_kept_across_the_interruption(self):
        app = self.start_trip("common")
        advance(app, 4)                                                                  # 2 s of the 6 s drive are left
        run(app, PersonEngaged(), UserChoice("done"))
        advance(app, 3)                                                                  # the goodbye (farewell_s)
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        self.assertAlmostEqual(app.dialogue.fsm.time_left(event="arrived"), 2.0, delta=0.6)

    def test_the_whole_trip_completes_after_an_interruption_and_all_books_get_home(self):
        app = self.start_trip("common")
        run(app, PersonEngaged(), UserChoice("done"))
        advance(app, 40)
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertTrue(app.dialogue.storage.empty)
        self.assertEqual(app.nav[-1].title, "home")

    def test_a_book_placed_during_a_trip_pauses_it_and_the_new_book_joins_the_next_plan(self):
        app = self.start_trip("common")
        run(app, BookPlaced())
        self.assertEqual(app.dialogue.state, "ENGAGED.SCAN")
        run(app, scanned(HITCH), UserUtterance("return"), BookDropped(), UserUtterance("bye"))
        self.assertEqual(len(app.dialogue.storage), 3)
        advance(app, 5)
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        advance(app, 60)
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertTrue(app.dialogue.storage.empty)

    def test_a_nearby_busy_person_gets_an_excuse_me_and_the_trip_carries_on(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                app = self.start_trip(zone)
                audio = app.actuators.voice.audio
                advance(app, 2)
                run(app, PersonNearbyBusy())
                self.assertEqual(app.dialogue.state, "RETURNING.ANNOUNCE")
                self.assertIn({"name": "announce"}, sent(app, "light_pattern"))          # the light pattern, both zones
                text = app.screens[-1].text
                self.assertTrue(text.startswith("Excuse me, I'm returning books to the "), text)
                self.assertIn("section.", text)
                self.assertEqual(app.screens[-1].state_path, "RETURNING.ANNOUNCE")
                self.assertFalse(any("Excuse me" in s for s in app.said))                # no conversation was started
                if zone == "common":
                    self.assertEqual(len(audio.played), 1)                               # the soft chime
                else:
                    self.assertEqual(audio.played, [])                                   # silence
                advance(app, 3)                                                          # announce_s is 3
                self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
                self.assertAlmostEqual(app.dialogue.fsm.time_left(event="arrived"), 4.0, delta=0.6)    # 6 - 2: the announcement was free
                self.assertEqual(len(app.nav), 1)                                        # and the base was not re-sent
                advance(app, 5)
                self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")

    def test_a_busy_person_at_the_shelf_gets_the_announcement_too_and_unloading_continues(self):
        app = self.start_trip("common")
        advance(app, 6.5)
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")
        run(app, PersonNearbyBusy())
        self.assertEqual(app.dialogue.state, "RETURNING.ANNOUNCE")
        advance(app, 4)
        self.assertEqual(app.dialogue.state, "RETURNING.AT_SHELF")

    def test_a_busy_person_does_nothing_when_the_robot_is_idle_or_talking_to_someone(self):
        for start in ("IDLE", "OFFER"):
            app = new_app()
            go(app, start)
            run(app, PersonNearbyBusy())
            self.assertEqual(app.dialogue.state, "IDLE" if start == "IDLE" else "ENGAGED.OFFER")

    def test_the_chime_is_a_valid_short_wav_made_with_the_standard_library(self):
        import io
        import wave
        from robot.actuation.chime import chime_wav
        with wave.open(io.BytesIO(chime_wav())) as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertLess(w.getnframes() / w.getframerate(), 2.0)
            self.assertGreater(w.getnframes(), 1000)


class BooksOnBoardTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_robot_holds_six_books_and_says_when_full(self):
        app = new_app()
        self.assertEqual(app.dialogue.storage.size, 6)
        self.assertEqual(app.state_info()["slots"], 6)
        for i in range(6):
            app.dialogue.storage.commit(app.dialogue.storage.reserve(), StoredBook(isbn=f"x{i}", title=f"Book {i}"))
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN))
        self.assertIn("storage is full", " ".join(app.said))

    def test_the_state_sent_to_the_operator_page_lists_the_books_held(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        info = app.state_info()
        self.assertEqual(info["stored"], 1)
        self.assertEqual(info["books"], [{"slot": 4, "title": "The Martian", "shelf": "2-SF-C", "section": "Science Fiction"}])

    def test_the_books_are_remembered_across_a_restart(self):
        from robot.app import build_storage
        from robot.config import load_config
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config()
            cfg.robot.setdefault("storage", {})["path"] = str(Path(d) / "storage.json")
            first = build_storage(cfg)
            first.commit(first.reserve(2), StoredBook(isbn="a", title="Dune"))
            again = build_storage(cfg)
            self.assertEqual((again.size, [b.title for b in again.stored()]), (6, ["Dune"]))


class OptionalBookTest(unittest.TestCase):
    """Only returning needs the book on the pad; browsing, summaries and recommendations do not."""

    def setUp(self):
        quiet_logs(self)

    def visit(self, *phrases, zone="common"):
        app = new_app(zone)
        run(app, PersonEngaged(), *[UserUtterance(p) for p in phrases])
        return app

    def test_a_section_or_topic_area_is_found_and_the_robot_offers_to_guide(self):
        for phrase, section in (("where is the history section?", "History"),
                                ("where are the astronomy books?", "Science"),
                                ("which floor is science fiction on", "Science Fiction")):
            with self.subTest(phrase=phrase):
                app = self.visit(phrase)
                self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
                self.assertIn(section, app.said[-1])
                self.assertIn("guide you there", app.said[-1])
                self.assertEqual(app.nav, [])
                run(app, UserUtterance("yes"))
                (event,) = app.nav
                self.assertEqual(event.section, section)

    def test_take_me_to_a_section_goes_straight_there(self):
        app = self.visit("take me to the science fiction section")
        (event,) = app.nav
        self.assertEqual((event.section, event.floor), ("Science Fiction", 2))
        self.assertIn("Okay, follow me to the Science Fiction section", app.said[-1])

    def test_a_book_title_is_enough_for_a_summary_and_for_recommendations(self):
        app = self.visit("tell me about the hitchhiker's guide to the galaxy")
        self.assertIn("whisked off Earth", " ".join(app.said))
        self.assertIsNone(app.dialogue.guide_target)
        app = self.visit("recommend something like dune")
        self.assertIn("If you like Dune, you might enjoy", " ".join(app.said))
        self.assertNotIn("Place", " ".join(app.said[2:]))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_general_talk_and_interests_need_no_book(self):
        app = self.visit("find me a book about space")
        self.assertIn("If you're interested in space", " ".join(app.said))
        app = self.visit("what is your name?")
        self.assertIn("I'm Stuart", app.said[-1])                                    # small talk, not a demand for a book

    def test_returning_still_needs_the_book_on_the_pad_even_if_a_title_was_named(self):
        app = self.visit("tell me about dune", "I want to return it")
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        self.assertIn("Place the book on my pad and I'll take it back", app.said[-1])
        self.assertTrue(app.dialogue.storage.empty)
        run(app, BookPlaced(), scanned(MARTIAN))
        self.assertEqual(app.dialogue.state, "ENGAGED.STORE")                        # now it can

    def test_a_named_book_does_not_pretend_to_be_on_the_pad(self):
        app = self.visit("tell me about dune")
        self.assertTrue(app.dialogue.session.virtual)
        run(app, UserChoice("more"))
        self.assertEqual(app.said[-1], __import__("robot.core.dialogue", fromlist=["x"]).OFFER_NO_BOOK)
        run(app, BookPlaced(), scanned(MARTIAN))
        self.assertFalse(app.dialogue.session.virtual)

    def test_asking_for_a_recommendation_with_no_book_asks_what_they_like_and_uses_the_answer(self):
        for zone in ZONES:
            for ask in ("can you recommend me some book?", "hmm i dont have the book just want some recommendation from you?"):
                with self.subTest(zone=zone, ask=ask):
                    app = self.visit(ask, zone=zone)
                    self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
                    self.assertIn("What kind of books do you like?", app.said[-1])
                    self.assertNotIn("Place the book on my pad", app.said[-1])
                    for answer, expected in (("space", "A Brief History of Time"), ("I like dystopian novels", "Nineteen Eighty-Four"),
                                             ("dune", "If you like Dune")):
                        again = self.visit(ask, answer, zone=zone)
                        self.assertEqual(again.dialogue.state, "ENGAGED.ANYTHING_ELSE", answer)
                        self.assertIn(expected, " ".join(again.said), answer)

    def test_asking_about_a_book_with_no_title_asks_which_one_then_explains_it(self):
        app = self.visit("tell me about a book")
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        self.assertIn("Which book?", app.said[-1])
        run(app, UserUtterance("cosmos"))
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")
        self.assertIn("journey through the universe", " ".join(app.said))

    def test_a_topic_nobody_has_is_said_so_and_the_visit_goes_on(self):
        app = self.visit("recommend a book", "zzzzz")
        self.assertIn("couldn't find a book about zzzzz", " ".join(app.said))

    def test_returning_is_still_the_only_one_that_insists_on_the_pad(self):
        app = self.visit("I want to return a book")
        self.assertIn("Place the book on my pad and I'll take it back", app.said[-1])

    def test_the_offer_mentions_finding_a_topic(self):
        self.assertIn("where a topic is", new_app().dialogue.offer_text())


class CameraLosesAnActiveVisitorTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_camera_losing_someone_who_just_spoke_does_not_end_the_visit(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where is dune?"), PersonLeft())
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")             # no goodbye
        self.assertFalse(any("Goodbye" in s for s in app.said))
        advance(app, 30)
        run(app, UserUtterance("recommend something like dune"))                   # still talking: still here
        self.assertIn("If you like Dune", " ".join(app.said))
        advance(app, 59)
        self.assertNotEqual(app.dialogue.state, "ENGAGED.FAREWELL")
        advance(app, 2)                                                            # quiet for leave_grace_s, no face
        self.assertIn(app.dialogue.state, ("ENGAGED.FAREWELL", "IDLE"))

    def test_the_face_coming_back_cancels_the_doubt(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where is dune?"), PersonLeft(), PersonEngaged())
        advance(app, 120)
        self.assertEqual(app.dialogue.state, "ENGAGED.ANYTHING_ELSE")

    def test_someone_who_never_said_anything_and_walked_off_gets_the_goodbye_at_once(self):
        app = new_app()
        run(app, PersonEngaged(), PersonLeft())
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")

    def test_the_operator_leave_always_ends_the_visit(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("where is dune?"), PersonLeft(source="operator"))
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")


class KnowsItsStorageTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    QUESTIONS = ("are you currently holding any books rn?", "what aobut do you ahve any in your storage",
                 "what books do you have?", "what's in your storage?")

    def test_an_empty_storage_is_said_to_be_empty_without_asking_the_llm(self):
        for question in self.QUESTIONS:
            app = new_app()
            run(app, PersonEngaged(), UserUtterance(question))
            self.assertIn("My storage is empty", app.said[-1], question)

    def test_it_names_the_books_it_holds_their_slots_and_shelves(self):
        app = new_app()
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped())
        for question in self.QUESTIONS:
            run(app, UserUtterance(question))
            answer = " ".join(app.said[-2:])
            self.assertIn("1 returned book", answer, question)
            self.assertIn("The Martian in slot 4", answer, question)
            self.assertIn("shelf 2-SF-C", answer, question)

    def test_with_a_book_on_the_pad_it_says_so_too(self):
        app = new_app()
        run(app, PersonEngaged(), BookPlaced(), scanned(MARTIAN), UserUtterance("what's in your storage?"))
        self.assertIn("The Martian is on my pad", " ".join(app.said[-2:]))

    def test_after_the_trip_back_the_storage_is_empty_again(self):
        app = stored_app()
        go(app, "PLAN")
        advance(app, 60)
        run(app, PersonEngaged(), UserUtterance("are you holding any books?"))
        self.assertIn("My storage is empty", app.said[-1])


class WherePhrasingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_a_loosely_typed_where_finds_the_book_not_a_section_in_its_title(self):
        app = new_app()
        run(app, PersonEngaged(), UserUtterance("oh i see can you tell me where the Brief history book?"))
        self.assertIn("A Brief History of Time is on shelf 3-SCI-A", " ".join(app.said))

    def test_a_bare_section_name_is_the_section(self):
        for phrase in ("where is history?", "where is the history section"):
            app = new_app()
            run(app, PersonEngaged(), UserUtterance(phrase))
            self.assertIn("The History section is on level 3", " ".join(app.said), phrase)

    def test_hi_right_after_the_goodbye_gets_the_hello_and_the_offer_not_a_second_hi(self):
        app = new_app()
        run(app, UserUtterance("hi"))
        self.assertEqual(app.said[0], "Hello! My name is Stuart.")
        self.assertIn("What would you like?", app.said[-1])
        self.assertEqual(len(app.said), 2)


class HoldBooksForTestingTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_with_the_configured_auto_start_off_returned_books_stay_in_storage(self):
        from robot.config import load_config
        self.assertFalse(load_config().returning["auto_start"])
        app = watch(make_app())                                                    # the real config
        run(app, PersonEngaged(), UserChoice("return"), BookPlaced(), scanned(MARTIAN), BookDropped(),
            UserUtterance("no thanks"))
        advance(app, 120)
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertEqual(app.dialogue.storage.slot_of(MARTIAN), 4)
        self.assertEqual(app.nav, [])
        go(app, "RETURNING")                                                       # the operator can still start a trip
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")


class TimeoutsOffTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_configured_timeouts_are_switched_off_so_the_robot_waits_for_the_answer(self):
        from robot.config import load_config
        self.assertFalse(load_config().timeouts["enabled"])
        for state in [s.name for s in SPEC.states.values() if s.waiting and s.name != "STORE"]:
            app = new_app()
            go(app, state)
            advance(app, 500)
            self.assertEqual(app.dialogue.state, SPEC.path(state), state)
            self.assertNotIn("ENGAGED.FAREWELL", [m.to_path for m in app.moves], state)

    def test_a_person_leaving_still_ends_the_visit_and_the_goodbye_still_times_out(self):
        app = new_app()
        run(app, PersonEngaged(), PersonLeft())
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
        advance(app, 5)
        self.assertEqual(app.dialogue.state, "IDLE")


class SpeakingDoesNotBlockTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_the_event_loop_keeps_running_while_the_robot_speaks_and_a_leave_is_handled_after(self):
        import time

        app = new_app()
        real = app.actuators.deliver

        def slow(text, style):
            time.sleep(0.4)                                    # a text-to-speech call and the playing of it
            return real(text, style)
        app.actuators.deliver = slow

        async def go():
            ticks = []

            async def other_work():                            # the camera, the display link, the ticker...
                for _ in range(8):
                    ticks.append(1)
                    await asyncio.sleep(0.05)

            task = asyncio.create_task(other_work())
            first = asyncio.create_task(app.bus.publish(PersonEngaged()))
            await asyncio.sleep(0.1)                           # the hello is being spoken now
            await app.bus.publish(PersonLeft())                # and the person leaves: queued, not lost
            await first
            await task
            return len(ticks)
        self.assertEqual(asyncio.run(go()), 8)
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")


class NextActionsAndLayoutTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_next_actions_list_only_what_can_happen_now_with_the_operator_command(self):
        app = new_app()
        app.dialogue.settings.timeouts["enabled"] = True
        run(app, PersonEngaged())
        by_label = {(a["label"], a["to"]): a for a in app.dialogue.next_actions()}
        self.assertEqual(by_label[("Visitor picks: return", "ENGAGED.AWAIT_BOOK")]["arg"], "return")       # no book yet
        self.assertNotIn(("Visitor picks: return", "ENGAGED.STORE"), by_label)
        self.assertEqual(by_label[("Person leaves", "ENGAGED.FAREWELL")]["command"], "leave")
        timeout = by_label[("Timeout", "ENGAGED.FAREWELL")]
        self.assertIsNone(timeout["command"])
        self.assertGreater(timeout["in_s"], 0)
        run(app, BookPlaced(), scanned(MARTIAN))                                                        # a book is on the pad
        by_label = {(a["label"], a["to"]): a for a in app.dialogue.next_actions()}
        self.assertEqual(by_label[("Visitor picks: return", "ENGAGED.STORE")]["why"], "a book is on the pad")
        self.assertNotIn(("Visitor picks: return", "ENGAGED.AWAIT_BOOK"), by_label)                    # the first match wins

    def test_every_next_action_leads_somewhere_the_table_allows_and_its_command_works(self):
        for state in SPEC.leaves():
            app = new_app()
            go(app, state)
            for action in app.dialogue.next_actions():
                self.assertIn(action["to"].split(" ")[0].split(".")[-1], SPEC.states, (state, action))
                if action["command"]:
                    self.assertIn(action["command"], ("engage", "busy", "leave", "book", "dropped", "pick"))

    def test_the_state_sent_to_the_page_has_the_map_and_the_next_actions(self):
        app = new_app()
        run(app, PersonEngaged())
        info = app.state_info()
        self.assertEqual(info["state"], "ENGAGED.OFFER")
        self.assertEqual([name for name, _ in info["tree"]], ["IDLE", "ENGAGED", "RETURNING"])
        self.assertTrue(info["next"])

    def test_a_common_area_keeps_the_face_full_screen_and_a_quiet_one_shows_the_text_panel(self):
        common, quiet = new_app("common"), new_app("quiet")
        for app in (common, quiet):
            run(app, PersonEngaged())
        self.assertEqual({u.layout for u in common.screens}, {"full_face"})
        self.assertIn("split", {u.layout for u in quiet.screens})
        offer = next(u for u in reversed(common.screens) if u.state_path == "ENGAGED.OFFER")
        self.assertTrue(offer.text)                                    # the text is still sent to the operator page

    def test_a_face_lost_for_a_few_seconds_does_not_end_the_visit(self):
        from robot.config import load_config
        self.assertGreaterEqual(load_config().engagement["left_after_s"], 10)


class OperatorGotoTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_goto_works_into_every_state_with_sensible_context(self):
        for state in SPEC.leaves():
            for zone in ZONES:
                with self.subTest(state=state, zone=zone):
                    app = new_app(zone)
                    path = asyncio.run(Operator(app).run("goto", state))
                    self.assertTrue(path)
                    expected = SPEC.path(state)
                    if state in ("PLAN", "ANNOUNCE", "HOMING"):                          # these pass straight through
                        self.assertTrue(app.dialogue.state.startswith("RETURNING"), app.dialogue.state)
                    elif state == "STORE":
                        self.assertIn(app.dialogue.state, (expected, "ENGAGED.ANYTHING_ELSE"))
                    elif state in ("GREET", "RECOMMEND", "ANSWER"):
                        self.assertTrue(app.dialogue.state.startswith("ENGAGED"), app.dialogue.state)
                    else:
                        self.assertEqual(app.dialogue.state, expected)
                    self.assertTrue(app.screens, state)

    def test_goto_fills_in_a_sample_book_for_scan_store_answer_and_recommend(self):
        for state in ("STORE", "ANSWER", "RECOMMEND"):
            app = new_app()
            go(app, state)
            self.assertIsNotNone(app.dialogue.session.book or app.dialogue.guide_target or app.said, state)
        app = new_app()
        go(app, "STORE")
        self.assertEqual(app.dialogue.state, "ENGAGED.STORE")
        self.assertIsNotNone(app.dialogue.store_slot)
        run(app, BookDropped(source="operator"))
        self.assertEqual(len(app.dialogue.storage), 1)

    def test_goto_returning_fills_two_stored_books_on_different_shelves(self):
        app = new_app()
        go(app, "RETURNING")
        self.assertEqual(app.dialogue.state, "RETURNING.NAVIGATING")
        self.assertEqual(len(app.dialogue.storage), 2)
        self.assertEqual(len(app.dialogue.storage.stops()), 2)
        advance(app, 40)
        self.assertEqual(app.dialogue.state, "IDLE")

    def test_goto_accepts_names_and_paths_and_says_what_it_does_not_know(self):
        app = new_app()
        said = []
        op = Operator(app, said.append)
        for text in ("offer", "ENGAGED.OFFER", "Await Book"):
            self.assertTrue(asyncio.run(op.run("goto", text)), text)
        self.assertEqual(app.dialogue.state, "ENGAGED.AWAIT_BOOK")
        self.assertFalse(asyncio.run(op.run("goto", "nowhere")))
        self.assertIn("no state called", said[-1])
        self.assertFalse(asyncio.run(op.run("goto", "")))
        self.assertFalse(asyncio.run(op.run("frobnicate")))

    def test_forced_moves_are_logged_with_the_operator_as_the_source(self):
        app = new_app()
        asyncio.run(Operator(app).run("goto", "OFFER"))
        self.assertEqual((app.moves[-1].event, app.moves[-1].source, app.moves[-1].to_path), ("force", "operator", "ENGAGED.OFFER"))

    def test_the_terminal_commands_make_the_events_they_name(self):
        app = new_app()
        op = Operator(app)
        for command, arg, state in (("engage", "", "ENGAGED.OFFER"), ("pick", "return", "ENGAGED.AWAIT_BOOK"),
                                    ("book", "", "ENGAGED.STORE"), ("dropped", "", "ENGAGED.ANYTHING_ELSE"),
                                    ("say", "no thanks", "ENGAGED.FAREWELL")):
            asyncio.run(op.run(command, arg))
            self.assertEqual(app.dialogue.state, state, command)
        self.assertEqual({m.source for m in app.moves if m.event in ("PersonEngaged", "Choice", "BookPlaced", "BookDropped")},
                         {"operator"})
        asyncio.run(op.run("reset", "all"))
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertTrue(app.dialogue.storage.empty)

    def test_leave_makes_the_person_go_and_busy_signals_a_nearby_person(self):
        app = stored_app()
        op = Operator(app)
        asyncio.run(op.run("engage"))
        asyncio.run(op.run("leave"))
        self.assertEqual(app.dialogue.state, "ENGAGED.FAREWELL")
        self.assertEqual(app.context.group_size, 0)
        asyncio.run(op.run("engage"))                                                    # and engaging brings someone back
        self.assertEqual(app.context.group_size, 1)

    def test_reset_returns_to_idle_forgetting_a_paused_trip_but_keeping_the_books(self):
        app = stored_app()
        go(app, "PLAN")
        run(app, PersonEngaged())
        asyncio.run(Operator(app).run("reset"))
        self.assertEqual(app.dialogue.state, "IDLE")
        self.assertEqual(len(app.dialogue.storage), 2)
        self.assertFalse(app.dialogue.returning_paused)
        run(app, PersonEngaged(), UserUtterance("bye"))
        advance(app, 20)                                                                 # no resume: a fresh plan from the top
        self.assertTrue(app.dialogue.state.startswith("RETURNING"))


if __name__ == "__main__":
    unittest.main()
