"""Held-out evaluation: `scenarios run --variants FILE` (robot/scenarios/heldout.py) and how it is marked."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from robot.config import load_config
from robot.scenarios import cli, report, runner
from robot.scenarios.heldout import apply_variants, heldout_scenarios, load_variants, say_steps
from robot.scenarios.spec import SCENARIO_DIR, ScenarioError, load_all
from tests.test_scenarios import NONE_LLM, quiet_logs


def write(directory: Path, data, name="phrases.yaml") -> Path:
    path = directory / name
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def all_steps(scenarios, name, text):
    return {n: [f"{text} {n}"] for n, _ in say_steps(scenarios[name].steps)}


class Base(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.scenarios = load_all()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class LoadTest(Base):
    def test_say_steps_are_numbered_in_file_order_through_branches(self):
        counts = {name: len(say_steps(s.steps)) for name, s in self.scenarios.items()}
        self.assertEqual(counts, {"ask_about_book": 6, "find_section": 10, "recommend": 4, "return_book": 5,
                                  "returning_interrupted": 3})

    def test_a_filled_file_loads(self):
        path = write(self.dir, {"recommend": all_steps(self.scenarios, "recommend", "anything like"), "find_section": None})
        found = load_variants(path, self.scenarios)
        self.assertEqual(list(found), ["recommend"])
        self.assertEqual(found["recommend"][2], ["anything like 2"])

    def test_the_template_is_valid_but_empty(self):
        template = SCENARIO_DIR / "heldout_template.yaml"
        with self.assertRaisesRegex(ScenarioError, "no phrasings"):
            load_variants(template, self.scenarios)
        data = yaml.safe_load(template.read_text(encoding="utf-8"))
        self.assertTrue(all(not phrases for steps in data.values() for phrases in steps.values()))

    def test_the_template_lists_every_say_step_of_every_scenario(self):
        data = yaml.safe_load((SCENARIO_DIR / "heldout_template.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), sorted(self.scenarios))
        for name, scenario in self.scenarios.items():
            self.assertEqual(sorted(data[name]), [n for n, _ in say_steps(scenario.steps)], name)

    def test_the_template_is_not_loaded_as_a_scenario(self):
        self.assertNotIn("heldout_template", self.scenarios)
        self.assertEqual(len(self.scenarios), 5)

    def test_problems_are_all_named(self):
        cases = {
            "not a scenario": {"nope": {1: ["x"]}},
            "are 1 to 4": {"recommend": {9: ["x"]}},
            "have no phrasings": {"recommend": {1: ["x"]}},
            "list of phrasings": {"recommend": {1: [1, 2], 2: ["a"], 3: ["a"], 4: ["a"]}},
            "braces": {"recommend": {1: ["{book}"], 2: ["a"], 3: ["a"], 4: ["a"]}},
        }
        for message, data in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(ScenarioError, message):
                    load_variants(write(self.dir, data), self.scenarios)

    def test_bad_files(self):
        with self.assertRaisesRegex(ScenarioError, "not valid YAML"):
            load_variants(write(self.dir, "a: [unclosed"), self.scenarios)
        with self.assertRaisesRegex(ScenarioError, "cannot read"):
            load_variants(self.dir / "missing.yaml", self.scenarios)
        with self.assertRaisesRegex(ScenarioError, "top level"):
            load_variants(write(self.dir, "- a\n- b"), self.scenarios)

    def test_title_and_author_placeholders_are_allowed(self):
        data = all_steps(self.scenarios, "ask_about_book", "x")
        data[1] = ["tell me about {title}", "what did {author} write?"]
        self.assertEqual(load_variants(write(self.dir, {"ask_about_book": data}), self.scenarios)["ask_about_book"][1], data[1])


class ApplyTest(Base):
    def test_only_the_text_is_replaced(self):
        original = self.scenarios["ask_about_book"]
        changed = apply_variants(original, load_variants(
            write(self.dir, {"ask_about_book": all_steps(self.scenarios, "ask_about_book", "held")}), self.scenarios)["ask_about_book"],
            "phrases.yaml")
        self.assertEqual(changed.heldout, "phrases.yaml")
        self.assertEqual(original.heldout, "")                                   # the loaded scenario is untouched
        self.assertEqual(changed.expect, original.expect)
        old, new = dict(say_steps(original.steps)), dict(say_steps(changed.steps))
        for n in old:
            text = lambda step: step["say"]["text"] if isinstance(step["say"], dict) else step["say"]
            self.assertEqual(text(new[n]), [f"held {n}"])
            if isinstance(old[n]["say"], dict):                                  # answerable / unanswerable flags stay
                self.assertEqual({k: v for k, v in new[n]["say"].items() if k != "text"},
                                 {k: v for k, v in old[n]["say"].items() if k != "text"})
        self.assertNotIn("held", json.dumps(original.steps))

    def test_only_filled_scenarios_are_returned(self):
        variants = load_variants(write(self.dir, {"recommend": all_steps(self.scenarios, "recommend", "z")}), self.scenarios)
        chosen = heldout_scenarios(self.scenarios, variants, "p.yaml")
        self.assertEqual(list(chosen), ["recommend"])

    def test_a_named_scenario_that_is_not_filled_in_is_an_error(self):
        path = write(self.dir, {"recommend": all_steps(self.scenarios, "recommend", "z")})
        with self.assertRaisesRegex(ScenarioError, "no phrasings for 'return_book'"):
            cli.heldout_set(path, self.scenarios, "return_book")
        self.assertEqual(list(cli.heldout_set(path, self.scenarios, "recommend")), ["recommend"])


class RunTest(Base):
    def run_batch(self, chosen, out):
        cfg = load_config()
        plan = runner.plan_batch(list(chosen), chosen, 1, ["common"], 3)
        return asyncio.run(runner.run_batch(plan, cfg, NONE_LLM, 20.0, 0.0, out, progress=lambda *_: None,
                                            speech_probability=0.0))

    def test_the_visitor_says_the_held_out_phrasing_and_the_batch_is_marked(self):
        phrases = write(self.dir, {"recommend": {1: ["zzz one"], 2: ["zzz two"], 3: ["zzz three"], 4: ["zzz four"]}})
        chosen = cli.heldout_set(phrases, self.scenarios, "all")
        out = self.dir / "run"
        records = self.run_batch(chosen, out)
        said = [e["text"] for r in records for e in r["timeline"] if e["kind"] == "step" and e.get("action") == "say"]
        self.assertTrue(said and all(t.startswith("zzz ") for t in said), said)
        self.assertTrue(all(r["heldout"] == "phrases.yaml" for r in records))
        self.assertEqual(json.loads((out / "meta.json").read_text())["heldout"], "phrases.yaml")
        summary, _ = report.write_report(out)
        text = summary.read_text(encoding="utf-8")
        self.assertIn("HELD-OUT batch", text.split("\n", 3)[2])
        self.assertIn("phrases.yaml", text)

    def test_a_tuned_batch_says_so_and_is_not_marked_held_out(self):
        chosen = {"recommend": self.scenarios["recommend"]}
        out = self.dir / "tuned"
        records = self.run_batch(chosen, out)
        self.assertTrue(all(r["heldout"] == "" for r in records))
        self.assertEqual(json.loads((out / "meta.json").read_text())["heldout"], "")
        report.write_report(out)
        text = (out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Tuned batch", text)
        self.assertNotIn("HELD-OUT", text)

    def test_compare_says_when_a_held_out_batch_meets_a_tuned_one(self):
        held = self.run_batch(heldout_scenarios(self.scenarios, load_variants(write(
            self.dir, {"recommend": all_steps(self.scenarios, "recommend", "qq")}), self.scenarios), "p.yaml"), self.dir / "a")
        tuned = self.run_batch({"recommend": self.scenarios["recommend"]}, self.dir / "b")
        text = report.compare_markdown(held, tuned, "a", "b")
        self.assertIn("a is a held-out batch and b is a tuned batch", text)
        self.assertNotIn("held-out", report.compare_markdown(tuned, tuned, "b", "b"))

    def test_the_cli_refuses_the_empty_template_and_runs_a_filled_file(self):
        self.assertEqual(cli.main(["run", "--variants", str(SCENARIO_DIR / "heldout_template.yaml")]), 2)
        phrases = write(self.dir, {"recommend": all_steps(self.scenarios, "recommend", "qq")})
        out = self.dir / "cli"
        code = cli.main(["run", "--variants", str(phrases), "--runs", "1", "--zones", "common", "--llm", "mock",
                         "--typos", "0", "--speech", "0", "--speed", "20", "--out", str(out)])
        self.assertIn(code, (0, 1))
        self.assertIn("HELD-OUT", (out / "summary.md").read_text(encoding="utf-8"))

    def test_replaying_a_held_out_run_needs_the_file(self):
        out = self.dir / "r"
        phrases = write(self.dir, {"recommend": {n: [f"zz {n}"] for n in range(1, 5)}})
        record = self.run_batch(cli.heldout_set(phrases, self.scenarios, "all"), out)[0]
        self.assertEqual(cli.main(["replay", record["run_id"], "--dir", str(out)]), 2)


if __name__ == "__main__":
    unittest.main()
