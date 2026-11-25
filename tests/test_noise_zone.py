"""The base's microphones and position deciding the zone: the noise filter (median, hysteresis, robot noise, calibration),
the zone resolver (pose, noise, frozen) and the app wiring."""

import logging
import unittest
from types import SimpleNamespace

from robot.actuation.face import FaceDisplay
from robot.app import build_app, build_plan, fake_drivers
from robot.config import load_config
from robot.core.zones import ZoneResolver, section_zones
from robot.events import (BookDropped, ContextChanged, NoiseReading, PersonEngaged, PersonLeft, PoseUpdated, UserChoice, UserUtterance)
from robot.perception.noise import NoiseFilter
from tests.test_dialogue import advance, run
from tests.test_returning_link import StubLink

SETTINGS = {"window_s": 45, "min_samples": 6, "to_common_above_db": 55, "to_quiet_below_db": 48, "uncalibrated": "ignore",
            "raw_thresholds": {"to_common_above_db": -35, "to_quiet_below_db": -45}, "enabled": True}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def reading(db, calibrated=True, robot_noise=False):
    return NoiseReading(db, db, db, 0.0, "dB" if calibrated else "dBFS", calibrated, robot_noise, 0.0)


def feed(f, clock, values, step=0.5, **kw):
    for v in values:
        clock.t += step
        f.add(reading(v, **kw))


class NoiseFilterTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.f = NoiseFilter(SETTINGS, self.clock)

    def test_no_answer_until_there_are_enough_readings(self):
        feed(self.f, self.clock, [70] * 5)
        self.assertEqual((self.f.median, self.f.zone), (None, None))
        feed(self.f, self.clock, [70])
        self.assertEqual((self.f.median, self.f.zone), (70, "common"))

    def test_the_median_ignores_a_burst(self):
        feed(self.f, self.clock, [40] * 20)
        self.assertEqual(self.f.zone, "quiet")
        feed(self.f, self.clock, [90, 95, 92])                                   # a door slam, a burst of speech
        self.assertEqual((self.f.median, self.f.zone), (40, "quiet"))

    def test_hysteresis_in_between_the_last_answer_stands(self):
        feed(self.f, self.clock, [40] * 8)
        self.assertEqual(self.f.zone, "quiet")
        feed(self.f, self.clock, [52] * 100)                                     # between 48 and 55: still quiet
        self.assertEqual((self.f.median, self.f.zone), (52, "quiet"))
        feed(self.f, self.clock, [58] * 100)                                     # the 45 s window has to fill with the new level
        self.assertEqual(self.f.zone, "common")
        feed(self.f, self.clock, [52] * 100)                                     # back in between: still common
        self.assertEqual((self.f.median, self.f.zone), (52, "common"))
        feed(self.f, self.clock, [44] * 100)
        self.assertEqual(self.f.zone, "quiet")

    def test_exactly_on_a_threshold_is_not_a_change(self):
        feed(self.f, self.clock, [40] * 8)
        feed(self.f, self.clock, [55] * 100)
        self.assertEqual(self.f.zone, "quiet")                                   # "above 55", not "at 55"
        feed(self.f, self.clock, [56] * 100)
        self.assertEqual(self.f.zone, "common")
        feed(self.f, self.clock, [48] * 100)
        self.assertEqual(self.f.zone, "common")                                  # "below 48", not "at 48"

    def test_old_readings_leave_the_window(self):
        feed(self.f, self.clock, [70] * 10)
        self.assertEqual(self.f.zone, "common")
        self.clock.t += 46
        self.assertEqual((self.f.median, self.f.zone), (None, None))             # silence from the base: no answer, the map stands
        feed(self.f, self.clock, [40] * 8)
        self.assertEqual(self.f.zone, "quiet")                                    # and it starts afresh

    def test_readings_made_while_the_robot_made_noise_are_ignored(self):
        feed(self.f, self.clock, [40] * 8)
        feed(self.f, self.clock, [80] * 30, robot_noise=True)
        self.assertEqual((self.f.median, self.f.zone), (40, "quiet"))
        self.assertEqual(self.f.ignored["robot_noise"], 30)
        self.assertEqual(self.f.accepted, 8)

    def test_uncalibrated_readings_are_ignored_by_default(self):
        feed(self.f, self.clock, [-20] * 10, calibrated=False)
        self.assertEqual((self.f.median, self.f.zone, self.f.accepted), (None, None, 0))
        self.assertEqual(self.f.ignored["uncalibrated"], 10)

    def test_uncalibrated_readings_can_be_compared_with_raw_thresholds(self):
        f = NoiseFilter({**SETTINGS, "uncalibrated": "raw"}, self.clock)
        feed(f, self.clock, [-50] * 8, calibrated=False)
        self.assertEqual(f.zone, "quiet")                                         # below -45
        feed(f, self.clock, [-30] * 100, calibrated=False)
        self.assertEqual(f.zone, "common")                                        # above -35
        feed(f, self.clock, [-40] * 100, calibrated=False)
        self.assertEqual(f.zone, "common")                                        # in between

    def test_dB_and_dBFS_are_never_mixed(self):
        f = NoiseFilter({**SETTINGS, "uncalibrated": "raw"}, self.clock)
        feed(f, self.clock, [60] * 8)
        self.assertEqual(f.zone, "common")
        feed(f, self.clock, [-50], calibrated=False)                              # the base lost its calibration
        self.assertEqual((f.median, f.zone), (None, None))                        # the old window is gone

    def test_it_can_be_switched_off(self):
        f = NoiseFilter({**SETTINGS, "enabled": False}, self.clock)
        feed(f, self.clock, [90] * 10)
        self.assertEqual((f.zone, f.accepted), (None, 0))

    def test_garbage_is_not_taken(self):
        for bad in (float("nan"), float("inf"), None, "loud"):
            self.assertFalse(self.f.add(SimpleNamespace(overall=bad, calibrated=True, robot_noise=False, unit="dB")))
        self.assertEqual(self.f.ignored["bad"], 4)

    def test_the_defaults_are_55_and_48_over_45_seconds(self):
        noise = load_config().noise
        self.assertEqual((noise["to_common_above_db"], noise["to_quiet_below_db"], noise["window_s"], noise["uncalibrated"]),
                         (55, 48, 45, "ignore"))


def cfg_with(**zones):
    cfg = load_config()
    cfg.zones.update(zones)
    return cfg


class ZoneResolverTest(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with(zone_from_pose=True, zone_dwell_s=3)
        self.clock = Clock()
        self.frozen = [False]
        plan = build_plan(self.cfg)
        graph = SimpleNamespace(shelves={"a": SimpleNamespace(section="Science", zone="quiet"),
                                         "b": SimpleNamespace(section="Science Fiction", zone="common")})
        self.z = ZoneResolver(self.cfg, plan, section_zones(graph), lambda: self.frozen[0], self.clock, level=3)

    def test_the_zone_comes_from_the_section_the_base_is_in(self):
        self.z.on_pose(4.0, 1.5, True)                                            # inside Science on level 3
        d = self.z.decide()
        self.assertEqual((d.zone, d.source, d.area), ("quiet", "map", "Science"))
        self.z.on_pose(10.0, 7.0, True)                                           # in no section: the default zone
        self.z.decide()                                                           # (the robot looks every fraction of a second)
        self.clock.t += 4
        self.assertEqual(self.z.decide().zone, "common")

    def test_a_new_zone_must_last_the_dwell_time(self):
        self.z.on_pose(4.0, 1.5, True)
        self.assertEqual(self.z.decide().zone, "quiet")
        self.z.on_pose(10.0, 7.0, True)
        self.clock.t += 1
        self.assertEqual(self.z.decide().zone, "quiet")                           # just passing the edge
        self.z.on_pose(4.0, 1.5, True)
        self.clock.t += 5
        self.assertEqual(self.z.decide().zone, "quiet")
        self.z.on_pose(10.0, 7.0, True)
        self.clock.t += 0.1
        self.z.decide()
        self.clock.t += 3.1
        self.assertEqual(self.z.decide().zone, "common")

    def test_not_localised_means_the_typed_zone_stands(self):
        self.z.set_typed("quiet")
        self.z.on_pose(10.0, 7.0, False)
        self.assertEqual((self.z.decide().zone, self.z.decide().source), ("quiet", "map"))

    def test_without_zone_from_pose_the_pose_is_not_used(self):
        z = ZoneResolver(cfg_with(zone_from_pose=False), build_plan(self.cfg), {}, lambda: False, self.clock, level=3)
        z.set_typed("quiet")
        z.on_pose(10.0, 7.0, True)
        self.assertEqual(z.decide().zone, "quiet")

    def test_noise_overrides_the_map_and_says_so(self):
        self.z.on_pose(4.0, 1.5, True)                                            # a quiet section
        for _ in range(10):
            self.clock.t += 0.5
            self.z.on_noise(reading(70))
        d = self.z.decide()
        self.assertEqual((d.zone, d.source, d.map_zone, d.noise_zone), ("common", "noise", "quiet", "common"))

    def test_a_quiet_room_makes_a_common_area_quiet(self):
        self.z.on_pose(10.0, 7.0, True)
        for _ in range(10):
            self.clock.t += 0.5
            self.z.on_noise(reading(35))
        self.assertEqual((self.z.decide().zone, self.z.decide().source), ("quiet", "noise"))

    def test_nothing_changes_while_a_visitor_is_being_helped_and_it_follows_the_room_again_after(self):
        self.z.on_pose(4.0, 1.5, True)
        self.assertEqual(self.z.decide().zone, "quiet")
        self.frozen[0] = True                                                      # a visit begins
        self.z.on_pose(10.0, 7.0, True)
        self.clock.t += 10
        for _ in range(10):
            self.clock.t += 0.5
            self.z.on_noise(reading(70))
        d = self.z.decide()
        self.assertEqual((d.zone, d.source, d.frozen), ("quiet", "frozen", True))
        self.frozen[0] = False                                                     # the visit is over
        d = self.z.decide()
        self.assertEqual((d.zone, d.source), ("common", "noise"))

    def test_section_zones_take_the_zone_most_of_the_shelves_have(self):
        graph = SimpleNamespace(shelves={i: SimpleNamespace(section="S", zone=z) for i, z in enumerate(["quiet", "common", "common"])})
        self.assertEqual(section_zones(graph), {"s": "common"})
        self.assertEqual(section_zones(None), {})

    def test_a_section_can_name_its_own_zone_in_the_floor_plan(self):
        from robot.services.floorplan import FloorPlan, FloorPlanError
        data = {"floors": [{"level": 1, "width": 10, "height": 10, "sections": [{"name": "Hush", "x": 0, "y": 0, "w": 5, "h": 5, "zone": "quiet"}]}]}
        plan = FloorPlan.from_dict(data)
        self.assertEqual(plan.floor(1).section("Hush").zone, "quiet")
        data["floors"][0]["sections"][0]["zone"] = "noisy"
        with self.assertRaises(FloorPlanError):
            FloorPlan.from_dict(data)
        z = ZoneResolver(cfg_with(zone_from_pose=True), FloorPlan.from_dict({**data, "floors": [{**data["floors"][0], "sections": [
            {"name": "Hush", "x": 0, "y": 0, "w": 5, "h": 5, "zone": "quiet"}]}]}), {}, lambda: False, self.clock, level=1)
        z.on_pose(1, 1, True)
        self.assertEqual(z.decide().zone, "quiet")


def zone_app(**zones):
    logging.disable(logging.INFO)
    cfg = load_config()
    cfg.zones.update(zones)
    cfg.robot.setdefault("books", {})["online_fallback"] = False
    cfg.robot.setdefault("screen", {})["greeting_hold_s"] = 0
    app = build_app(cfg, fake_drivers(), face=FaceDisplay(enabled=False), log_sessions=False, persist_storage=False, link=StubLink())
    app.now = [1000.0]
    app.dialogue.clock = lambda: app.now[0]
    app.zones.clock = lambda: app.now[0]
    app.zones.noise.clock = lambda: app.now[0]
    app.zones.level = 3
    return app


def noisy(app, db, n=10):
    for _ in range(n):
        app.now[0] += 0.5
        run(app, reading(db))


class AppZoneTest(unittest.TestCase):
    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_moving_from_a_quiet_area_to_a_common_one_switches_the_style_while_idle(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("common", 1))
        run(app, PoseUpdated(4.0, 1.5, 0.0, True))                                 # the Science section: quiet
        self.assertEqual((app.context.zone, app.zone_source), ("quiet", "map"))
        self.assertEqual(app.style.voice, "whisper")
        run(app, PoseUpdated(10.0, 7.0, 0.0, True))                                # the corridor: common
        self.assertEqual((app.context.zone, app.style.voice), ("common", "normal"))

    def test_the_noise_turns_a_quiet_area_common_and_the_context_shows_the_median(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("common", 1), PoseUpdated(4.0, 1.5, 0.0, True))
        self.assertEqual(app.context.zone, "quiet")
        noisy(app, 70)
        self.assertEqual((app.context.zone, app.zone_source, app.context.noise_db), ("common", "noise", 70))

    def test_a_robots_own_voice_does_not_make_the_room_loud(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("common", 1), PoseUpdated(4.0, 1.5, 0.0, True))
        for _ in range(30):
            app.now[0] += 0.5
            run(app, reading(85, robot_noise=True))
        self.assertEqual(app.context.zone, "quiet")

    def test_the_zone_is_frozen_while_a_visitor_is_being_helped_and_updates_afterwards(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("common", 1), PoseUpdated(4.0, 1.5, 0.0, True))
        self.assertEqual(app.context.zone, "quiet")
        run(app, PersonEngaged())
        self.assertTrue(app.dialogue.fsm.is_in("ENGAGED"))
        run(app, PoseUpdated(10.0, 7.0, 0.0, True))                                # the base is moved to a common area, or the room fills
        noisy(app, 75)
        self.assertEqual((app.context.zone, app.zone_source), ("quiet", "frozen"))
        self.assertEqual(app.state_info()["link"]["zone"]["source"], "frozen")
        run(app, UserChoice("done"))                                               # the visit ends (FAREWELL, then IDLE)
        advance(app, 6)
        self.assertEqual(app.dialogue.state, "IDLE")
        asyncio_run(app.refresh_zone())
        self.assertEqual((app.context.zone, app.zone_source), ("common", "noise"))

    def test_the_head_count_still_changes_the_style_while_the_zone_is_frozen(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("common", 1), PoseUpdated(4.0, 1.5, 0.0, True), PersonEngaged())
        run(app, ContextChanged("common", 3))                                       # the people counter: three people
        self.assertEqual((app.context.zone, app.context.social), ("quiet", "group"))

    def test_the_typed_zone_is_the_fallback_until_the_base_is_localised(self):
        app = zone_app(zone_from_pose=True, zone_dwell_s=0)
        run(app, ContextChanged("quiet", 1))
        self.assertEqual(app.context.zone, "quiet")
        run(app, PoseUpdated(10.0, 7.0, 0.0, False))                                # not localised: the pose means nothing
        self.assertEqual(app.context.zone, "quiet")
        run(app, PoseUpdated(10.0, 7.0, 0.0, True))
        self.assertEqual(app.context.zone, "common")

    def test_the_simulator_without_a_link_is_unchanged(self):
        from tests.test_dialogue import make_app
        app = make_app()
        self.assertIsNone(app.zones)
        run(app, ContextChanged("quiet", 1, 62.0))                                  # the sim's `noise 62`: the older instantaneous override
        self.assertEqual(app.context.zone, "common")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
