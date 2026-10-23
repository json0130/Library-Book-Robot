"""The scenario test harness (robot/scenarios): spec loading, variants, the clock, replay, each check, report and compare."""

import asyncio
import csv
import json
import logging
import random
import tempfile
import unittest
from pathlib import Path

from robot.config import load_config
from robot.scenarios import checks as C
from robot.scenarios import report, runner
from robot.scenarios.clock import ScaledClock
from robot.scenarios.spec import Scenario, ScenarioError, load_all, load_scenario, parse_scenario
from robot.scenarios.variants import add_typos, choose_words, pick

NONE_LLM = ("none", None, "none")


def quiet_logs(test):
    logging.disable(logging.CRITICAL)
    test.addCleanup(logging.disable, logging.NOTSET)


def scenario(**overrides):
    data = {"name": "t", "steps": ["engage"], "expect": []}
    data.update(overrides)
    return parse_scenario(data, "t")


class SpecTest(unittest.TestCase):
    def test_the_five_scenarios_load(self):
        found = load_all()
        self.assertEqual(sorted(found), ["ask_about_book", "find_section", "recommend", "return_book", "returning_interrupted"])
        for s in found.values():
            self.assertEqual(s.zones, ("quiet", "common"))
            self.assertTrue(s.description and s.steps and s.expect)

    def test_bad_scenarios_name_every_problem(self):
        for data, needle in (
                ({"name": "Bad Name", "steps": ["engage"]}, "`name` must be"),
                ({"name": "t"}, "`steps` must be a non-empty list"),
                ({"name": "t", "steps": ["jump"]}, "'jump' is not a step"),
                ({"name": "t", "steps": [{"say": []}]}, "say needs a text"),
                ({"name": "t", "steps": [{"place_book": "nonsense"}]}, "place_book needs"),
                ({"name": "t", "steps": [{"wait": {"until_state": "X"}}]}, "wait needs seconds"),
                ({"name": "t", "steps": [{"tap": 3}]}, "tap needs"),
                ({"name": "t", "steps": [{"one_of": [{"name": "a", "steps": ["engage"]}]}]}, "at least two branches"),
                ({"name": "t", "steps": [{"maybe": {"name": "a", "p": 2, "steps": ["engage"]}}]}, "p between 0 and 1"),
                ({"name": "t", "steps": ["engage"], "zones": ["roof"]}, "`zones` must be"),
                ({"name": "t", "steps": ["engage"], "expect": [{"views": "book_card"}]}, "needs a list of names"),
                ({"name": "t", "steps": ["engage"], "expect": [{"wat": 1}]}, "needs exactly one of"),
                ({"name": "t", "steps": ["engage"], "expect": [{"view_field": {"view": "x", "field": "y", "matches": "("}}]},
                 "bad regular expression"),
                ({"name": "t", "steps": ["engage"], "colour": "red"}, "unknown key"),
                ({"name": "t", "steps": [{"say": {"text": "x", "answerable": True, "unanswerable": True}}]}, "not both")):
            with self.assertRaises(ScenarioError, msg=data) as caught:
                parse_scenario(data, "f.yaml")
            self.assertIn(needle, str(caught.exception))
            self.assertIn("f.yaml", str(caught.exception))
        with self.assertRaises(ScenarioError) as caught:                         # several problems in one message
            parse_scenario({"name": "x y", "steps": ["jump"], "zones": ["roof"]}, "f.yaml")
        for needle in ("`name` must be", "`zones` must be", "'jump' is not a step"):
            self.assertIn(needle, str(caught.exception))

    def test_files_that_are_not_yaml_or_missing(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.yaml"
            bad.write_text("name: [")
            with self.assertRaises(ScenarioError):
                load_scenario(bad)
            with self.assertRaises(ScenarioError):
                load_scenario(Path(d) / "missing.yaml")
            for n in ("a", "b"):
                (Path(d) / f"{n}.yaml").write_text("name: same\nsteps: [engage]\n")
            with self.assertRaises(ScenarioError) as caught:
                load_all(d)
            self.assertIn("also used by", str(caught.exception))


class VariantsTest(unittest.TestCase):
    def test_the_same_seed_gives_the_same_wording_and_typos(self):
        options = ["I'd like to return a book", "can I return a book?", "I want to return this book"]
        runs = [[choose_words(options, random.Random(seed), 0.5) for _ in range(5)] for seed in (7, 7, 8)]
        self.assertEqual(runs[0], runs[1])
        self.assertNotEqual(runs[0], runs[2])

    def test_every_paraphrase_gets_picked_across_seeds(self):
        options = ["a one", "b two", "c three"]
        self.assertEqual({pick(options, random.Random(s)) for s in range(40)}, set(options))
        self.assertEqual(pick("only", random.Random(1)), "only")
        with self.assertRaises(ValueError):
            pick([], random.Random(1))

    def test_typos_keep_the_text_readable(self):
        text = "can you recommend something about astronomy please"
        changed = 0
        for seed in range(60):
            typed = add_typos(text, random.Random(seed))
            self.assertEqual(len(typed.split()), len(text.split()))
            self.assertEqual(typed.split()[0], "can")                              # short words are left alone
            for a, b in zip(text.split(), typed.split()):
                self.assertEqual(a[0], b[0])                                       # the first letter never changes
                self.assertLessEqual(abs(len(a) - len(b)), 1)
            changed += typed != text
        self.assertGreater(changed, 50)
        self.assertEqual(add_typos("a b c", random.Random(1)), "a b c")           # nothing long enough to mistype

    def test_typo_probability_is_respected(self):
        options = ["return the library book now"]
        never = [choose_words(options, random.Random(s), 0.0)[2] for s in range(30)]
        always = [choose_words(options, random.Random(s), 1.0)[2] for s in range(30)]
        self.assertFalse(any(never))
        self.assertTrue(all(always))
        some = sum(choose_words(options, random.Random(s), 0.2)[2] for s in range(500))
        self.assertTrue(60 < some < 140)

    def test_placeholders_are_filled_before_typos(self):
        text, base, _ = choose_words(["tell me about {title}"], random.Random(1), 0.0, title="Dune")
        self.assertEqual((text, base), ("tell me about Dune", "tell me about {title}"))


class ClockTest(unittest.TestCase):
    def test_the_clock_runs_faster_than_real_time(self):
        now = [100.0]
        clock = ScaledClock(10, start=1000.0, real=lambda: now[0])
        self.assertEqual(clock(), 1000.0)
        now[0] += 2.5
        self.assertEqual(clock(), 1025.0)
        self.assertEqual(clock.real_seconds(30), 3.0)

    def test_sleep_waits_a_tenth_at_ten_times_speed(self):
        import time
        clock = ScaledClock(10)
        started = time.monotonic()
        asyncio.run(clock.sleep(1.0))
        self.assertTrue(0.08 < time.monotonic() - started < 0.4)
        with self.assertRaises(ValueError):
            ScaledClock(0)

    def test_the_robots_timers_use_the_injected_clock(self):
        """A farewell lasts 3 robot-seconds: at speed 20 that is well under a real second."""
        import time
        quiet_logs(self)
        from robot.events import PersonEngaged, PersonLeft
        from robot.scenarios.runner import build_robot
        clock = ScaledClock(20)
        app, _ = build_robot(load_config(), None, clock, {})

        async def go():
            await app.bus.publish(PersonEngaged())
            await app.bus.publish(PersonLeft(source="operator"))
            started = time.monotonic()
            while app.dialogue.state != "IDLE" and time.monotonic() - started < 3:
                await app.dialogue.tick()
                await asyncio.sleep(0.01)
            return time.monotonic() - started
        self.assertLess(asyncio.run(go()), 0.9)
        self.assertEqual(app.dialogue.state, "IDLE")


class BatchPlanTest(unittest.TestCase):
    def test_runs_are_split_evenly_over_the_zones(self):
        self.assertEqual(runner.zone_split(10, ["quiet", "common"]), [5, 5])
        self.assertEqual(runner.zone_split(3, ["quiet", "common"]), [2, 1])
        self.assertEqual(runner.zone_split(10, ["common"]), [10])
        scenarios = load_all()
        plan = runner.plan_batch(["return_book"], scenarios, 10, ["quiet", "common"], 1)
        self.assertEqual([z for _, z, _, _ in plan].count("quiet"), 5)
        self.assertEqual(len({seed for _, _, seed, _ in plan}), 10)
        self.assertEqual(len({rid for *_, rid in plan}), 10)
        self.assertEqual(plan, runner.plan_batch(["return_book"], scenarios, 10, ["quiet", "common"], 1))
        self.assertNotEqual([s for _, _, s, _ in plan], [s for _, _, s, _ in runner.plan_batch(["return_book"], scenarios, 10, ["quiet", "common"], 2)])


class ReplayTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def play(self, name, zone, seed, typos=0.5):
        s = load_all()[name]
        return asyncio.run(runner.play(s, zone, seed, f"{name}-{zone}-001", load_config(), NONE_LLM, 40, typos))

    def test_the_same_seed_replays_the_same_choices_and_state_path(self):
        a = self.play("return_book", "common", 4242)
        b = self.play("return_book", "common", 4242)
        self.assertEqual(a["choices"], b["choices"])
        self.assertEqual(C.state_path(a["timeline"]), C.state_path(b["timeline"]))
        self.assertEqual([e["text"] for e in a["timeline"] if e["kind"] == "step" and e.get("text")],
                         [e["text"] for e in b["timeline"] if e["kind"] == "step" and e.get("text")])

    def test_different_seeds_make_different_choices(self):
        choices = {json.dumps(self.play("return_book", "common", seed)["choices"]) for seed in range(1, 6)}
        self.assertGreater(len(choices), 1)

    def test_a_run_records_everything_it_should(self):
        run = self.play("return_book", "quiet", 5, typos=0.0)
        kinds = {e["kind"] for e in run["timeline"]}
        self.assertTrue({"step", "state", "said", "screen", "cmd"} <= kinds)
        for key in ("scenario", "run_id", "seed", "zone", "choices", "checks", "thresholds", "events", "final_state"):
            self.assertIn(key, run)
        self.assertEqual([e["n"] for e in run["timeline"]], list(range(1, len(run["timeline"]) + 1)))
        said = [e for e in run["timeline"] if e["kind"] == "said"]
        self.assertTrue(said and all(e["voice"] == "none" and not e["spoken"] for e in said))      # quiet: nothing aloud
        self.assertEqual(json.loads(json.dumps(run))["run_id"], run["run_id"])                       # JSON-serialisable

    def test_replay_command_reruns_a_recorded_run(self):
        from robot.scenarios import cli
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "batch"
            plan = runner.plan_batch(["return_book"], load_all(), 2, ["common"], 3)
            asyncio.run(runner.run_batch(plan, load_config(), NONE_LLM, 40, 0.5, out, progress=lambda *_: None))
            old = {r["run_id"]: r for r in runner.load_runs(out)}
            rid = plan[0][3]
            replayed = asyncio.run(runner.play(plan[0][0], "common", old[rid]["seed"], rid, load_config(), NONE_LLM, 40,
                                               old[rid]["typo_probability"]))
            self.assertEqual(replayed["choices"], old[rid]["choices"])
            self.assertTrue(runner.transcript_path(out, rid).is_file())


# ---------------------------------------------------------------------------------------------- checks
def entry(n, kind, t=0.0, rt=None, **fields):
    return {"n": n, "t": t, "rt": t if rt is None else rt, "kind": kind, **fields}


def run_of(timeline, zone="common", **extra):
    base = {"run_id": "r-1", "scenario": "t", "zone": zone, "seed": 1, "speed": 1, "llm": "none", "tags": [], "choices": [],
            "final_state": "IDLE", "stuck": [], "error": None, "storage": 0, "timeline": timeline, "events": [],
            "thresholds": {"first_screen_ms": 500, "first_sentence_ms": 3000}, "catalogue_titles": ["Dune"]}
    base.update(extra)
    return base


def step(n, action="say", rt=0.0, **fields):
    return entry(n, "step", rt=rt, step=n, action=action, detail=f"{action} {n}", **fields)


def screen(n, rt=0.0, view="face", **key):
    return entry(n, "screen", rt=rt, view=view, layout="full_face", state_path="IDLE", zone="common", key=key)


def said(n, text="hello", rt=0.0, voice="normal", spoken=True):
    return entry(n, "said", rt=rt, text=text, voice=voice, spoken=spoken, channels=["speech"])


class GenericChecksTest(unittest.TestCase):
    def test_no_exceptions(self):
        self.assertTrue(C.no_exceptions(run_of([step(1), screen(2)])).passed)
        failed = C.no_exceptions(run_of([step(1), entry(2, "error", where="run", message="boom")]))
        self.assertEqual((failed.passed, failed.line), (False, 2))
        self.assertIn("boom", failed.reason)
        warning = entry(3, "log", level="WARNING", logger="robot.x", message="slow", exception=False)
        self.assertTrue(C.no_exceptions(run_of([warning])).passed)                      # a warning alone is not a bug
        self.assertFalse(C.no_exceptions(run_of([entry(3, "log", level="ERROR", logger="robot.x", message="bad", exception=True)])).passed)

    def test_no_stuck_state(self):
        self.assertTrue(C.no_stuck_state(run_of([])).passed)
        self.assertFalse(C.no_stuck_state(run_of([entry(1, "error", where="wait", message="x")], stuck=["still in OFFER"])).passed)
        self.assertIn("ended in ENGAGED.OFFER", C.no_stuck_state(run_of([], final_state="ENGAGED.OFFER")).reason)
        self.assertFalse(C.no_stuck_state(run_of([], error="the run took more than 240 real seconds")).passed)

    def test_quiet_zone_voice_and_chime(self):
        spoke_first = run_of([said(1, "Hello"), step(2)], zone="quiet")
        self.assertFalse(C.quiet_zone_voice(spoke_first).passed)
        user_first = run_of([step(1), said(2, "Hello")], zone="quiet")
        self.assertTrue(C.quiet_zone_voice(user_first).passed)
        silent = run_of([said(1, "Hello", voice="none", spoken=False)], zone="quiet")
        self.assertTrue(C.quiet_zone_voice(silent).passed)
        chime = run_of([step(1), entry(2, "chime", volume=0.3)], zone="quiet")
        self.assertEqual((C.quiet_zone_voice(chime).passed, C.quiet_zone_voice(chime).line), (False, 2))
        self.assertIsNone(C.quiet_zone_voice(run_of([said(1)], zone="common")))

    def test_screen_before_speech(self):
        good = run_of([step(1), screen(2), said(3)])
        self.assertTrue(C.screen_before_speech(good).passed)
        late = C.screen_before_speech(run_of([step(1), said(2), screen(3)]))
        self.assertFalse(late.passed)
        self.assertIn("after the sentence", late.reason)
        self.assertEqual(late.line, 2)
        never = C.screen_before_speech(run_of([step(1), said(2)]))
        self.assertIn("never changed", never.reason)
        self.assertTrue(C.screen_before_speech(run_of([step(1), screen(2)])).passed)    # no sentence: nothing to order

    def test_latency_thresholds_come_from_the_record(self):
        fast = run_of([step(1, rt=1.0), screen(2, rt=1.1), said(3, rt=1.2)])
        self.assertTrue(all(r.passed for r in C.latency(fast)))
        slow_screen = run_of([step(1, rt=1.0), screen(2, rt=1.9), said(3, rt=2.0)])
        results = {r.name: r for r in C.latency(slow_screen)}
        self.assertFalse(results["latency_first_screen"].passed)
        self.assertIn("900 ms", results["latency_first_screen"].reason)
        self.assertTrue(results["latency_first_sentence"].passed)
        slow_speech = run_of([step(1, rt=1.0), screen(2, rt=1.1), said(3, rt=5.0)])
        self.assertFalse({r.name: r for r in C.latency(slow_speech)}["latency_first_sentence"].passed)
        loose = run_of([step(1, rt=1.0), screen(2, rt=1.9)], thresholds={"first_screen_ms": 2000, "first_sentence_ms": 3000})
        self.assertTrue(all(r.passed for r in C.latency(loose)))

    def test_no_needless_sorry(self):
        self.assertIsNone(C.no_needless_sorry(run_of([step(1), said(2, "Sorry")])))     # not marked answerable
        bad = run_of([step(1, answerable=True), said(2, "Sorry, I'm not sure how to answer that.")])
        self.assertFalse(C.no_needless_sorry(bad).passed)
        good = run_of([step(1, answerable=True), said(2, "The Martian was written by Andy Weir.")])
        self.assertTrue(C.no_needless_sorry(good).passed)

    def test_honest_about_unknowns(self):
        asked = dict(unanswerable=True)
        self.assertTrue(C.honest_about_unknowns(run_of([step(1, **asked), said(2, "I'm not sure, the catalogue has no page count.")])).passed)
        invented = C.honest_about_unknowns(run_of([step(1, **asked), said(2, "It has 384 pages.")]))
        self.assertFalse(invented.passed)
        self.assertIn("made-up number", invented.reason)
        self.assertFalse(C.honest_about_unknowns(run_of([step(1, **asked), said(2, "It is a great read!")])).passed)
        self.assertFalse(C.honest_about_unknowns(run_of([step(1, **asked)])).passed)      # no reply at all
        echoed = C.honest_about_unknowns(run_of([step(1, **asked), said(2, "[MOCK] Received: System: You are Stuart")]))
        self.assertTrue(echoed.passed)                                                    # the mock cannot be judged
        self.assertIn("mock LLM", echoed.reason)

    def test_tap_found(self):
        self.assertTrue(C.taps_found(run_of([step(1, "tap")])).passed)
        self.assertFalse(C.taps_found(run_of([step(1, "tap", error="no button 'x' on the screen")])).passed)


class ExpectationTest(unittest.TestCase):
    def check(self, item, timeline, **extra):
        return C.evaluate(item, run_of(timeline, **extra))

    def states(self, *paths):
        return [entry(i + 1, "state", **{"from": "", "to": p, "event": "e", "source": "sensor", "zone": "common", "internal": False})
                for i, p in enumerate(paths)]

    def test_state_path_is_an_ordered_subsequence_by_leaf_name_or_full_path(self):
        t = self.states("IDLE", "ENGAGED.GREET", "ENGAGED.OFFER", "ENGAGED.SCAN", "ENGAGED.STORE", "ENGAGED.FAREWELL")
        self.assertTrue(self.check({"state_path": ["SCAN", "ENGAGED.STORE", "FAREWELL"]}, t).passed)
        wrong_order = self.check({"state_path": ["STORE", "SCAN"]}, t)
        self.assertFalse(wrong_order.passed)
        self.assertIn("SCAN was not reached in order", wrong_order.reason)
        self.assertFalse(self.check({"state_path": ["RETURNING.NAVIGATING"]}, t).passed)

    def test_views_phrases_and_events(self):
        t = [screen(1, view="book_card"), said(2, "Done: it is in storage slot 3."), screen(3, view="directions")]
        self.assertTrue(self.check({"views": ["book_card", "directions"]}, t).passed)
        self.assertIn("book_list", self.check({"views": ["book_list"]}, t).reason)
        forbidden = self.check({"forbid_phrases": ["Slot 3"]}, t)
        self.assertEqual((forbidden.passed, forbidden.line), (False, 2))
        self.assertTrue(self.check({"forbid_phrases": ["guide you"]}, t).passed)
        self.assertTrue(self.check({"require_phrases_any": ["guide", "slot 3"]}, t).passed)
        self.assertFalse(self.check({"require_phrases_any": ["guide"]}, t).passed)
        events = [(1, "NavigationRequested")]
        self.assertTrue(self.check({"events": ["NavigationRequested"]}, t, events=events).passed)
        self.assertFalse(self.check({"events": ["NavigationRequested"]}, t).passed)
        self.assertTrue(self.check({"no_events": ["NavigationRequested"]}, t).passed)
        self.assertFalse(self.check({"no_events": ["NavigationRequested"]}, t, events=events).passed)

    def test_view_field(self):
        t = [screen(1, view="book_card", badge="Storage slot 4")]
        self.assertTrue(self.check({"view_field": {"view": "book_card", "field": "badge", "matches": "slot"}}, t).passed)
        self.assertFalse(self.check({"view_field": {"view": "book_card", "field": "badge", "matches": "shelf"}}, t).passed)
        self.assertFalse(self.check({"view_field": {"view": "directions", "field": "badge", "matches": "slot"}}, t).passed)

    def test_chime(self):
        with_chime = [entry(1, "chime", volume=0.4)]
        self.assertTrue(self.check({"chime": True}, with_chime).passed)
        self.assertFalse(self.check({"chime": False}, with_chime).passed)
        self.assertFalse(self.check({"chime": True}, []).passed)
        self.assertTrue(self.check({"chime": False}, []).passed)

    def test_book_list_valid(self):
        good = [screen(1, view="book_list", items=[{"isbn": "1", "title": "Dune", "reason": "Like X, it's about space"}])]
        self.assertTrue(self.check({"book_list_valid": True}, good).passed)
        unknown = [screen(1, view="book_list", items=[{"isbn": "1", "title": "Invented Book", "reason": "x"}])]
        self.assertIn("not in the catalogue", self.check({"book_list_valid": True}, unknown).reason)
        no_reason = [screen(1, view="book_list", items=[{"isbn": "1", "title": "Dune", "reason": ""}])]
        self.assertIn("no reason", self.check({"book_list_valid": True}, no_reason).reason)
        many = [screen(1, view="book_list", items=[{"isbn": "1", "title": "Dune", "reason": "because a; because b"}])]
        self.assertIn("more than one reason", self.check({"book_list_valid": True}, many).reason)
        self.assertFalse(self.check({"book_list_valid": True}, []).passed)

    def test_silent_farewell(self):
        def state(n, to):
            return entry(n, "state", **{"from": "x", "to": to, "event": "e", "source": "s", "zone": "q", "internal": False})
        quiet = [state(1, "ENGAGED.FAREWELL"), entry(2, "cmd", cmd="light_pattern", params={"name": "pulse"}), state(3, "IDLE")]
        self.assertTrue(self.check({"silent_farewell": True}, quiet, zone="quiet").passed)
        spoken = [state(1, "ENGAGED.FAREWELL"), said(2, "Goodbye"), state(3, "IDLE")]
        self.assertFalse(self.check({"silent_farewell": True}, spoken, zone="quiet").passed)
        self.assertTrue(self.check({"silent_farewell": False}, spoken).passed)
        self.assertFalse(self.check({"silent_farewell": False}, quiet).passed)
        self.assertFalse(self.check({"silent_farewell": True}, [state(1, "IDLE")]).passed)

    def test_resume_same_shelf(self):
        def state(n, a, b):
            return entry(n, "state", **{"from": a, "to": b, "event": "e", "source": "s", "zone": "c", "internal": False})

        def nav(n, shelf):
            return entry(n, "nav", title=f"shelf {shelf}", isbn="", shelf=shelf, section="x", floor=2, x=1, y=1)
        same = [nav(1, "2-SF-A"), state(2, "RETURNING.NAVIGATING", "ENGAGED.GREET"), state(3, "ENGAGED.FAREWELL", "RETURNING.NAVIGATING"),
                nav(4, "2-SF-A")]
        self.assertTrue(C.resume_same_shelf(run_of(same)).passed)
        other = same[:3] + [nav(4, "3-HIS-A")]
        self.assertIn("resumed towards 3-HIS-A", C.resume_same_shelf(run_of(other)).reason)
        self.assertFalse(C.resume_same_shelf(run_of([nav(1, "2-SF-A")])).passed)

    def test_when_unless_and_zones_decide_whether_an_item_applies(self):
        run = run_of([], tags=["unknown_book"])
        self.assertFalse(C.applies({"views": ["x"], "unless": "unknown_book"}, run))
        self.assertTrue(C.applies({"views": ["x"], "when": "unknown_book"}, run))
        self.assertFalse(C.applies({"views": ["x"], "when": "other"}, run))
        self.assertTrue(C.applies({"views": ["x"], "zones": ["common"]}, run))
        self.assertFalse(C.applies({"views": ["x"], "zones": ["quiet"]}, run))
        s = scenario(expect=[{"views": ["book_card"], "zones": ["quiet"]}, {"views": ["book_card"]}])
        results = C.run_checks(run_of([], zone="common"), s)
        self.assertEqual(sum(r.name == "expect:views" for r in results), 1)


class ReportTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def batch(self, name, runs=4, seed=1, llm=NONE_LLM):
        out = Path(self.tmp.name) / name
        plan = runner.plan_batch(["return_book"], load_all(), runs, ["quiet", "common"], seed)
        asyncio.run(runner.run_batch(plan, load_config(), llm, 40, 0.2, out, progress=lambda *_: None))
        return out

    def test_summary_review_sheet_and_transcripts(self):
        out = self.batch("a")
        summary, review = report.write_report(out)
        text = summary.read_text()
        for needle in ("# Scenario run summary", "## Pass rate by scenario and zone", "| return_book | quiet |",
                       "| return_book | common |", "## Failing checks", "## Distinct robot replies", "median"):
            self.assertIn(needle, text)
        rows = list(csv.DictReader(open(review, encoding="utf-8")))
        self.assertEqual(len(rows), 4)
        self.assertEqual(list(rows[0]), ["scenario", "zone", "run_id", "transcript", "automatic_result", "rating_1_to_5", "notes"])
        self.assertTrue(all(r["rating_1_to_5"] == "" and r["notes"] == "" for r in rows))
        for row in rows:
            transcript = (out / row["transcript"]).read_text()
            self.assertIn(f"# {row['run_id']}", transcript)
            self.assertIn("## Timeline", transcript)
            self.assertIn("## Checks", transcript)

    def test_failing_checks_link_to_transcript_lines(self):
        records = [run_of([step(1), said(2, "x"), screen(3)], run_id="t-quiet-001", scenario="t", zone="quiet"),
                   run_of([step(1), screen(2), said(3, "x")], run_id="t-quiet-002", scenario="t", zone="quiet")]
        for r in records:
            r["checks"] = [c.to_json() for c in C.run_checks(r, scenario())]
            r["passed"] = all(c["passed"] for c in r["checks"])
        text = report.summary_markdown(records, Path("x"))
        self.assertIn("**screen_before_speech**: 1 of 2 runs", text)
        self.assertIn("transcripts/t-quiet-001.md#L002", text)
        self.assertIn("1/2 (50%)", text)

    def test_distinct_replies_to_the_same_input(self):
        def record(rid, reply):
            return run_of([step(1, paraphrase="who wrote it?"), screen(2), said(3, reply)], run_id=rid, scenario="ask")
        records = [record("a", "Andy Weir."), record("b", "Andy Weir."), record("c", "It was written by Andy Weir.")]
        variety = report.reply_variety(records)
        ((key, counter),) = variety.items()
        self.assertEqual(key, ("ask", "who wrote it?"))
        self.assertEqual(counter["Andy Weir."], 2)
        self.assertEqual(report.reply_variety(records[:2]), {})

    def test_percentiles_and_llm_fallback_rate(self):
        self.assertEqual(report.percentile([10, 20, 30, 40, 50], 50), 30)
        self.assertEqual(report.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 90), 90)
        self.assertIsNone(report.percentile([], 50))
        r = run_of([entry(1, "llm", phase="used", method="explain", reply="x"),
                    entry(2, "llm", phase="fallback", method="recommend", reply=""),
                    entry(3, "llm", phase="error", error="down", ms=5)])
        self.assertEqual(report.llm_outcomes(r), (1, 2))
        r["checks"], r["passed"] = [], True
        self.assertAlmostEqual(report.stats([r])["fallback_rate"], 2 / 3)

    def test_compare_shows_the_changes(self):
        a = self.batch("a", runs=4, seed=1)
        b = self.batch("b", runs=4, seed=1)
        text = report.compare(a, b)
        for needle in ("# Compare a -> b", "pass rate:", "first screen median", "LLM fallback rate", "| return_book | quiet |",
                       "Checks that now pass / now fail"):
            self.assertIn(needle, text)
        better = [json.loads(json.dumps(r)) for r in runner.load_runs(a)]
        worse = [json.loads(json.dumps(r)) for r in runner.load_runs(a)]
        for r in better:
            for c in r["checks"]:
                c["passed"] = True
            r["passed"] = True
        for r in worse:
            r["checks"].append({"name": "invented", "passed": False, "reason": "x", "line": None})
            r["passed"] = False
        text = report.compare_markdown(worse, better, "worse", "better")
        self.assertIn("now passing: invented (4 fewer failing run(s))", text)
        self.assertIn("0% (", text.split("| return_book | common |")[1])
        self.assertIn("100%", text)
        only = report.compare_markdown(worse[:2], better, "w", "b")
        self.assertIn("only in b", only)


class RunnerBehaviourTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def play(self, steps, zone="common", setup=None, typos=0.0, seed=1):
        s = scenario(steps=steps, setup=setup or {}, expect=[])
        return asyncio.run(runner.play(s, zone, seed, "t-1", load_config(), NONE_LLM, 40, typos))

    def test_a_missing_button_is_reported_not_crashed(self):
        run = self.play(["engage", {"tap": "No Such Button"}, {"wait": 0.1}])
        failed = [c for c in run["checks"] if c["name"] == "tap_found"]
        self.assertFalse(failed[0]["passed"])
        self.assertIn("no button", failed[0]["reason"])
        self.assertIsNone(run["error"])

    def test_a_wait_that_never_ends_is_marked_stuck(self):
        run = self.play(["engage", {"wait": {"until_state": "RETURNING.HOMING", "timeout": 2}}])
        self.assertTrue(run["stuck"])
        stuck = next(c for c in run["checks"] if c["name"] == "no_stuck_state")
        self.assertFalse(stuck["passed"])
        self.assertIn("never reached RETURNING.HOMING", stuck["reason"])

    def test_setup_stores_books_and_start_returning_runs_the_trip(self):
        run = self.play(["start_returning", {"wait": {"until_state": "IDLE", "timeout": 120}}], setup={"stored": 2})
        self.assertEqual(run["storage"], 0)
        self.assertEqual(run["final_state"], "IDLE")
        self.assertEqual(sum(e["kind"] == "nav" for e in run["timeline"]), 3)           # two shelves and home

    def test_branches_and_maybe_are_chosen_from_the_seed_and_tagged(self):
        steps = ["engage", {"one_of": [{"name": "a", "steps": [{"wait": 0.1}]}, {"name": "b", "steps": [{"wait": 0.1}]}]},
                 {"maybe": {"name": "sometimes", "p": 0.5, "steps": [{"wait": 0.1}]}}]
        seen = set()
        for seed in range(12):
            run = self.play(steps, seed=seed)
            self.assertEqual(run, self.play(steps, seed=seed) | {k: run[k] for k in ("timeline", "started", "checks")})
            seen |= {tag for tag in run["tags"]}
        self.assertEqual(seen, {"a", "b", "sometimes"})

    def test_an_unknown_isbn_is_not_stored(self):
        run = self.play(["engage", {"say": "I want to return a book"}, {"place_book": "unknown"}, {"wait": 1}])
        self.assertIn("unknown_book", run["tags"])
        self.assertEqual(run["storage"], 0)

    def test_paraphrases_are_recorded_with_typos(self):
        run = self.play(["engage", {"say": ["where is the history section please"]}], typos=1.0)
        said_step = next(e for e in run["timeline"] if e["kind"] == "step" and e.get("text"))
        self.assertTrue(said_step["typos"])
        self.assertNotEqual(said_step["text"], said_step["paraphrase"])


class LiveCardsTest(unittest.TestCase):
    def test_cards_are_chosen_from_the_seed(self):
        from robot.scenarios.live import cards_for
        books = load_config() and __import__("robot.app", fromlist=["x"]).build_finder(load_config()).catalog.books
        s = load_all()["return_book"]
        a = cards_for(s, random.Random(9), books, 0.3)
        b = cards_for(s, random.Random(9), books, 0.3)
        self.assertEqual(a, b)
        cards, choices = a
        self.assertEqual([c["n"] for c in cards], list(range(1, len(cards) + 1)))
        self.assertTrue(any(c["kind"] == "place_book" for c in cards))
        self.assertTrue(any("say" in c for c in cards if c["kind"] == "say"))
        self.assertTrue(any(c["type"] == "branch" for c in choices))

    def test_a_dry_run_records_a_live_run(self):
        quiet_logs(self)
        import io
        from robot.scenarios.live import LiveGuide, cards_for
        from robot.sim.keyboard import run_sim
        cfg = load_config()
        s = load_all()["return_book"]
        with tempfile.TemporaryDirectory() as d:
            made = []

            def factory(app, sim, drivers):
                guide = LiveGuide(s, "common", 5, app, drivers, Path(d), "none", False, sim.say)
                made.append(guide)
                return guide
            script = "engage\nsay I want to return this book\nbook\ndropped\nnext\nnext\nnext\nnext\nabort\n"
            asyncio.run(run_sim(cfg, io.StringIO(script), io.StringIO(), log_sessions=False, use_llm=False, use_camera=False,
                                scenario_guide=factory, initial_zone="common", persist_storage=False, settle_s=0))
            (record,) = runner.load_runs(d)
            self.assertTrue(record["live"] and record["aborted"])
            self.assertTrue(any(e["kind"] == "step" and e.get("action") == "say" for e in record["timeline"]))
            self.assertTrue((Path(d) / "transcripts" / f"{record['run_id']}.md").is_file())
            self.assertFalse((Path(d) / "audio").exists())                              # no audio unless asked


if __name__ == "__main__":
    unittest.main()
