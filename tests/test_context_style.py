import unittest

from robot.config import load_config
from robot.core.context import Context, NoiseThresholds, derive_context, resolve_area
from robot.core.style import StylePolicy, StyleVector

T = NoiseThresholds(quiet_below_db=45, common_above_db=60)


class ContextTest(unittest.TestCase):
    def test_alone_and_group(self):
        self.assertEqual(derive_context("quiet", 1).social, "alone")
        self.assertEqual(derive_context("quiet", 0).social, "alone")      # nobody yet
        self.assertEqual(derive_context("quiet", 2).social, "group")
        self.assertEqual(derive_context("common", 5).key, "common_group")
        self.assertEqual(derive_context("quiet", -3).group_size, 0)

    def test_noise_overrides_map_zone_outside_the_band(self):
        self.assertEqual(derive_context("quiet", 1, 62, T).zone, "common")   # loud quiet area
        self.assertEqual(derive_context("common", 1, 40, T).zone, "quiet")   # silent common area
        self.assertEqual(derive_context("quiet", 1, 60, T).zone, "common")   # thresholds inclusive
        self.assertEqual(derive_context("common", 1, 45, T).zone, "quiet")
        for zone in ("quiet", "common"):                                     # in the band: map wins
            self.assertEqual(derive_context(zone, 1, 52, T).zone, zone)
            self.assertEqual(derive_context(zone, 1, None, T).zone, zone)

    def test_unknown_zone_and_areas(self):
        self.assertEqual(derive_context("rooftop", 1, default_zone="common").zone, "common")
        zones = load_config().zones
        self.assertEqual(resolve_area("reading_room", zones), "quiet")
        self.assertEqual(resolve_area("foyer", zones), "common")
        self.assertEqual(resolve_area("quiet", zones), "quiet")
        self.assertEqual(resolve_area("nowhere", zones), zones["default_zone"])
        self.assertEqual(NoiseThresholds.from_zones(zones), NoiseThresholds(45, 60))


class StyleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = StylePolicy(load_config().styles)

    def style(self, zone, people, fixed=False):
        return self.policy.style_for(Context(zone, group_size=people), fixed=fixed)

    def test_quiet_alone(self):
        s = self.style("quiet", 1)
        self.assertEqual((s.name, s.voice, s.channels), ("quiet_alone", "whisper", ("gaze", "spotlight", "print")))
        self.assertEqual((s.neck_pose, s.screen_layout, s.proactive), ("lowered", "split", False))
        self.assertLess(s.brightness, 0.5)

    def test_quiet_group(self):
        s = self.style("quiet", 3)
        self.assertEqual((s.voice, s.channels, s.neck_pose, s.screen_layout, s.proactive),
                         ("whisper", ("screen", "spotlight"), "lowered", "split", False))

    def test_common_alone(self):
        s = self.style("common", 1)
        self.assertEqual((s.voice, s.channels, s.neck_pose, s.screen_layout, s.proactive),
                         ("normal", ("speech", "screen"), "upright", "full_face", True))

    def test_common_group_is_louder_and_brighter(self):
        alone, group = self.style("common", 1), self.style("common", 4)
        self.assertEqual((group.voice, group.channels, group.proactive), ("normal", ("speech", "screen"), True))
        self.assertGreater(group.volume, alone.volume)
        self.assertGreater(group.brightness, alone.brightness)

    def test_fixed_mode_ignores_context(self):
        styles = {self.style(z, n, fixed=True) for z in ("quiet", "common") for n in (1, 3)}
        self.assertEqual(len(styles), 1)
        self.assertEqual(styles.pop().name, "fixed")

    def test_unknown_combination_falls_back_to_default(self):
        s = self.policy.style_for(Context("rooftop", group_size=1))
        self.assertEqual(s.name, "default")
        self.assertEqual((s.voice, s.proactive), ("whisper", False))
        self.assertEqual(self.policy.style_for(None).name, "default")

    def test_missing_fields_and_empty_table(self):
        policy = StylePolicy({"quiet_alone": {"voice": "normal"}})
        s = policy.style_for(Context("quiet", group_size=1))
        self.assertEqual(s.voice, "normal")
        self.assertEqual(s.neck_pose, StyleVector().neck_pose)          # filled from the default
        self.assertEqual(StylePolicy({}).style_for(Context("common")).name, "default")
        self.assertEqual(StylePolicy({}).style_for(Context("common"), fixed=True).name, "default")
        with self.assertRaises(ValueError):
            StylePolicy({"quiet_alone": {"voice": "shout"}})


if __name__ == "__main__":
    unittest.main()
