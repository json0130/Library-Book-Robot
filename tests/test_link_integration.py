"""The whole robot driving through the real link code to tools/fake_base.py: the simulator's --base, the scenario harness's
--base fake, the operator page's data, and the study log not drowning in pose and noise events."""

import asyncio
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robot.app import SAMPLED_EVENTS, build_app, fake_drivers
from robot.actuation.face import FaceDisplay
from robot.config import load_config
from robot.core.session_log import SessionLog
from robot.events import NoiseReading, PoseUpdated
from robot.scenarios import runner
from robot.scenarios.spec import load_all
from robot.sim.base import start_fake_base
from robot.sim.keyboard import apply_overrides, run_sim

NONE_LLM = ("none", None, "none")


class SimBaseTest(unittest.TestCase):
    def sim(self, lines, **kw):
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0
        apply_overrides(cfg, ["returning.auto_start=true", "returning.auto_start_delay_s=0.5", "timeouts.farewell_s=0.2", "returning.sim_unload_s=0.3"])
        out = io.StringIO()
        code = asyncio.run(run_sim(cfg, io.StringIO(lines), out, log_sessions=False, persist_storage=False, settle_s=40,
                                   base="fake", base_speed=10.0, **kw))
        return code, out.getvalue()

    def test_a_returning_trip_runs_through_the_link_and_the_base_line_is_shown(self):
        code, out = self.sim("goto RETURNING\nbase\n")
        self.assertEqual(code, 0)
        self.assertIn("base: http://127.0.0.1:", out)
        self.assertIn("(fake_base.py, in this process)", out)
        self.assertRegex(out, r"base:\s+link connected, at \(")
        self.assertIn("[nav] goal g-", out)
        self.assertIn("goal g-", out)
        self.assertIn("RETURNING.HOMING", out)
        self.assertIn("arrived at", out)

    def test_without_the_option_the_simulator_still_uses_timers(self):
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        out = io.StringIO()
        asyncio.run(run_sim(cfg, io.StringIO("base\n"), out, log_sessions=False, persist_storage=False, settle_s=0))
        self.assertIn("base:    none (the simulated drive)", out.getvalue())
        self.assertNotIn("127.0.0.1", out.getvalue())

    def test_the_turtlebot_backend_without_a_token_says_what_is_missing(self):
        cfg = load_config()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr", err):
            os.environ.pop("ROBOT_LINK_TOKEN", None)
            code = asyncio.run(run_sim(cfg, io.StringIO(""), io.StringIO(), log_sessions=False, persist_storage=False, base="turtlebot"))
        self.assertEqual(code, 1)
        self.assertIn("ROBOT_LINK_TOKEN", err.getvalue())

    def test_config_set_changes_a_setting_and_rejects_nonsense(self):
        cfg = load_config()
        apply_overrides(cfg, ["returning.auto_start=true", "turtlebot.base_url=http://x:1", "navigation.backend=turtlebot"])
        self.assertEqual((cfg.returning["auto_start"], cfg.turtlebot["base_url"], cfg.navigation["backend"]), (True, "http://x:1", "turtlebot"))
        apply_overrides(cfg, ["zones.zone_from_pose=true", "zones.noise.window_s=6"])          # config/zones.yaml
        self.assertEqual((cfg.zones["zone_from_pose"], cfg.noise["window_s"]), (True, 6))
        for bad in ("nonsense", "a..b=1", "=1", "zones=1"):
            with self.assertRaises(ValueError):
                apply_overrides(cfg, [bad])


class ScenarioBaseTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def play(self, name, zone, base):
        s = load_all(Path(__file__).resolve().parent.parent / "scenarios")[name]
        return asyncio.run(runner.play(s, zone, 5, f"{name}-{zone}-001", load_config(), NONE_LLM, 40, 0.0, speech_probability=0.0, base=base))

    def test_a_returning_scenario_through_the_link_passes_and_the_arrivals_came_from_the_base(self):
        record = self.play("returning_interrupted", "common", "fake")
        self.assertTrue(record["passed"], [c for c in record["checks"] if not c["passed"]])
        self.assertEqual(record["base"], "fake")
        arrivals = [e for e in record["timeline"] if e.get("kind") == "state" and e.get("event") in ("arrived", "home")]
        self.assertTrue(arrivals and all(e["source"] == "sensor" for e in arrivals), arrivals)      # the base said so, not a timer
        self.assertEqual(record["final_state"], "IDLE")
        self.assertEqual(record["storage"], 0)

    def test_the_default_base_is_the_simulator_and_arrivals_are_timers(self):
        record = self.play("returning_interrupted", "quiet", "sim")
        self.assertEqual(record["base"], "sim")
        arrivals = [e for e in record["timeline"] if e.get("kind") == "state" and e.get("event") in ("arrived", "home")]
        self.assertTrue(arrivals and all(e["source"] == "timeout" for e in arrivals))

    def test_the_cli_takes_base_fake(self):
        from robot.scenarios.cli import build_parser
        args = build_parser().parse_args(["run", "--base", "fake"])
        self.assertEqual(args.base, "fake")
        self.assertEqual(build_parser().parse_args(["run"]).base, "sim")


class OperatorAndLogTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_the_operator_page_gets_the_link_pose_drive_noise_and_zone(self):
        cfg = load_config()
        cfg.robot.setdefault("books", {})["online_fallback"] = False
        fake = start_fake_base(cfg, speed=5.0)
        self.addCleanup(fake.stop)
        link = fake.make_link(cfg)

        async def go():
            app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False, link=link)
            await app.start_link()
            try:
                for _ in range(100):
                    info = app.state_info()["link"]
                    if info.get("pose") and info.get("noise"):
                        break
                    await asyncio.sleep(0.05)
                return app.state_info()
            finally:
                await app.stop_link()

        info = asyncio.run(go())["link"]
        self.assertEqual((info["backend"], info["status"]), ("turtlebot", "connected"))
        self.assertEqual(set(info["pose"]) >= {"x", "y", "yaw", "localised"}, True)
        self.assertEqual(info["nav"]["status"], "idle")
        self.assertEqual(info["noise"]["unit"], "dB")
        self.assertEqual(info["zone"]["source"], "map")
        self.assertIn("skipped", info["returning"])
        self.assertTrue(json.dumps(info))                                                     # it goes to the page as JSON

    def test_the_simulator_has_no_base_section_data(self):
        from tests.test_dialogue import make_app
        info = make_app().state_info()["link"]
        self.assertEqual(info["backend"], "sim")
        self.assertNotIn("zone", info)

    def test_the_study_log_keeps_one_pose_and_noise_event_in_five_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            from tests.test_dialogue import make_app, run
            app = make_app(log_dir=d)
            now = [100.0]
            with mock.patch("robot.app.time.monotonic", lambda: now[0]):
                for i in range(30):
                    now[0] += 0.5
                    run(app, PoseUpdated(1.0, 2.0, 0.0, True), NoiseReading(40.0, t=float(i)))
            lines = [json.loads(l) for l in Path(app.session_log.path).read_text().splitlines()]
            names = [l["event"] for l in lines]
            self.assertEqual(names.count("PoseUpdated"), 3)                                    # 15 seconds of events
            self.assertEqual(names.count("NoiseReading"), 3)
        self.assertIn(PoseUpdated, SAMPLED_EVENTS)


if __name__ == "__main__":
    unittest.main()
