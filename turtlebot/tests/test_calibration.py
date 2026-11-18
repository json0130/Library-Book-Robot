import json
import os
import tempfile
import unittest

from support import Clock  # noqa: F401  (puts turtlebot/ on the path)
import calibrate_noise as cn
from robot_link import calibration

# What `amixer -c 0 scontents` printed on the Dell for the two USB microphones
AMIXER_REAL = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 147
  Mono: Capture 147 [100%] [-0.94dB] [on]
Simple mixer control 'Auto Gain Control',0
  Capabilities: pswitch pswitch-joined
  Playback channels: Mono
  Mono: Playback [off]
"""


class FitTest(unittest.TestCase):
    def points(self, offset_l=90.0, offset_r=91.0, refs=(40.0, 60.0, 78.0), noise=(0.0, 0.0, 0.0)):
        return [{"reference": r, "left": r - offset_l + n, "right": r - offset_r - n, "overall": 0.0} for r, n in zip(refs, noise)]

    def test_exact_data_gives_exact_offsets_and_no_error(self):
        fit = calibration.fit(self.points())
        self.assertEqual((fit["left_offset_db"], fit["right_offset_db"]), (90.0, 91.0))
        self.assertEqual(fit["max_error_db"], 0.0)
        self.assertEqual(fit["warnings"], [])
        self.assertEqual([r["error_db"] for r in fit["points"]], [0.0, 0.0, 0.0])

    def test_noisy_data_reports_the_error(self):
        fit = calibration.fit(self.points(noise=(1.0, -1.0, 2.0)))
        self.assertGreater(fit["max_error_db"], 0.5)
        self.assertLess(fit["max_error_db"], 3.0)
        self.assertEqual(fit["warnings"], [])

    def test_a_response_that_is_not_a_straight_line_is_warned_about(self):
        pts = self.points()
        pts[2]["left"] -= 8.0                                  # clipping at the loud end: the microphone stops following
        fit = calibration.fit(pts, tolerance_db=3.0)
        self.assertTrue(any("largest error" in w for w in fit["warnings"]))
        self.assertTrue(any("left microphone" in w and "slope" in w for w in fit["warnings"]))

    def test_it_needs_three_levels_that_are_far_apart(self):
        with self.assertRaises(ValueError):
            calibration.fit(self.points()[:2])
        with self.assertRaises(ValueError):
            calibration.fit(self.points(refs=(50.0, 52.0, 55.0)))

    def test_the_combined_level_is_the_power_mean(self):
        cal = calibration.Calibration(90.0, 90.0)
        self.assertEqual(cal.apply(-50.0, -50.0)["overall"], 40.0)
        self.assertAlmostEqual(cal.apply(-60.0, -50.0)["overall"], 37.4, delta=0.1)
        self.assertEqual(cal.apply(-50.0, -52.0)["balance"], 2.0)


class FileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "noise_calibration.json")

    def fit(self):
        pts = [{"reference": r, "left": r - 90, "right": r - 91, "overall": r - 90.5} for r in (40.0, 60.0, 78.0)]
        return calibration.fit(pts)

    def test_save_and_load(self):
        calibration.save(self.path, self.fit(), "average", {"left": {"volume_pct": 70}})
        cal = calibration.load(self.path)
        self.assertEqual((cal.left_offset_db, cal.right_offset_db, cal.level_mode), (90.0, 91.0, "average"))
        self.assertEqual(cal.mixer, {"left": {"volume_pct": 70}})
        with open(self.path) as f:
            body = json.load(f)
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["points"]), 3)

    def test_the_old_file_is_kept_as_bak(self):
        calibration.save(self.path, self.fit())
        calibration.save(self.path, self.fit())
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_no_file_is_none_and_a_broken_file_is_an_error(self):
        self.assertIsNone(calibration.load(self.path))
        for text in ("not json", "[]", "{}", '{"left_offset_db": 1}', '{"left_offset_db": "x", "right_offset_db": 1}',
                     '{"left_offset_db": 1e9, "right_offset_db": 1}', '{"left_offset_db": NaN, "right_offset_db": 1}'):
            with open(self.path, "w") as f:
                f.write(text)
            with self.assertRaises(calibration.CalibrationError, msg=text):
                calibration.load(self.path)

    def test_reloader_follows_the_file(self):
        r = calibration.Reloader(self.path)
        self.assertIsNone(r.poll())
        calibration.save(self.path, self.fit())
        self.assertEqual(r.poll().left_offset_db, 90.0)
        with open(self.path, "w") as f:
            f.write("garbage that is longer than before, so the size changes too")
        self.assertIsNone(r.poll())
        self.assertIn("cannot be read", r.error)
        os.remove(self.path)
        self.assertIsNone(r.poll())
        self.assertEqual(r.error, "")


class MixerTest(unittest.TestCase):
    def test_parse_the_real_output(self):
        state = cn.parse_mixer(AMIXER_REAL)
        self.assertEqual((state["volume_pct"], state["capture_on"], state["agc"], state["control"]), (100, True, False, "Mic"))

    def test_advice_for_the_real_state_is_to_lower_the_volume_only(self):
        problems, commands = cn.mixer_advice(0, cn.parse_mixer(AMIXER_REAL))
        self.assertEqual(len(problems), 1)
        self.assertEqual(commands, ["amixer -c 0 sset 'Mic' 70%"])

    def test_agc_on_and_muted_are_reported_with_commands(self):
        text = AMIXER_REAL.replace("Playback [off]", "Playback [on]").replace("[100%] [-0.94dB] [on]", "[50%] [-6dB] [off]")
        problems, commands = cn.mixer_advice(2, cn.parse_mixer(text))
        self.assertEqual(len(problems), 2)
        self.assertIn("amixer -c 2 cset name='Auto Gain Control' off", commands)
        self.assertIn("amixer -c 2 cset name='Mic Capture Switch' on", commands)

    def test_a_sensible_state_has_no_advice(self):
        text = AMIXER_REAL.replace("[100%] [-0.94dB]", "[70%] [-5dB]")
        self.assertEqual(cn.mixer_advice(0, cn.parse_mixer(text)), ([], []))


class GuidedSessionTest(unittest.TestCase):
    def session(self, refs, left_dbfs, right_dbfs, spread=1.0, tolerance=3.0, force=False):
        measured = iter(zip(left_dbfs, right_dbfs))
        said = []

        def collect(seconds):
            l, r = next(measured)
            return {"overall": (l + r) / 2, "left": l, "right": r, "spread": spread, "n": 20}

        def ask(prompt):
            if "What does the phone show" in prompt:
                return str(refs.pop(0))
            return ""
        return cn.run(collect, ask, said.append, 10, tolerance, force), said

    def test_a_good_session_is_accepted(self):
        result, said = self.session([42.0, 61.0, 77.0], [-48.0, -29.0, -13.0], [-49.0, -30.0, -14.0])
        self.assertTrue(result["accepted"])
        self.assertLess(result["max_error_db"], 1.0)
        self.assertTrue(any("Result" in line for line in said))

    def test_a_bad_session_is_not_accepted_without_force(self):
        result, said = self.session([42.0, 61.0, 77.0], [-48.0, -29.0, -28.0], [-49.0, -30.0, -29.0])
        self.assertFalse(result["accepted"])
        self.assertTrue(result["warnings"])
        again, _ = self.session([42.0, 61.0, 77.0], [-48.0, -29.0, -28.0], [-49.0, -30.0, -29.0], force=True)
        self.assertTrue(again["accepted"])

    def test_a_reading_that_is_not_a_number_is_asked_again(self):
        answers = iter(["abc", "5", "42", "61", "77"])
        levels = iter([(-48.0, -49.0)] * 3 + [(-29.0, -30.0), (-13.0, -14.0)])      # a wrong answer measures that level again

        def collect(seconds):
            l, r = next(levels)
            return {"overall": l, "left": l, "right": r, "spread": 0.5, "n": 20}

        def ask(prompt):
            return next(answers) if "What does the phone show" in prompt else ""
        result = cn.run(collect, ask, lambda *_: None, 10)
        self.assertEqual([p["reference"] for p in result["points"]], [42.0, 61.0, 77.0])

    def test_too_few_readings_is_an_error(self):
        with self.assertRaises(RuntimeError):
            cn.summarise([[-40, -40, -40]])
        s = cn.summarise([[-40, -41, -39]] * 3 + [[-30, -31, -29]] * 3)
        self.assertEqual(s["n"], 6)


class OneMicrophoneTest(unittest.TestCase):
    """A mono mic_config.json ({"mono": {"usb_port": ...}}): one microphone is both the left and the right one."""

    def test_cards_come_from_the_mono_entry_or_from_left_and_right(self):
        find = {"1-1": 4, "1-2.6": 0, "1-2.5": 1}.get
        self.assertEqual(cn.cards_from_config({"mono": {"usb_port": "1-1"}}, find), {"microphone": 4})
        self.assertEqual(cn.cards_from_config({"left": {"usb_port": "1-2.6"}, "right": {"usb_port": "1-2.5"}}, find), {"left": 0, "right": 1})
        self.assertEqual(cn.cards_from_config({"mono": {"usb_port": "1-9"}}, find), {"microphone": None})        # not plugged in

    def test_setup_mics_writes_a_mono_config_for_the_port_given(self):
        import setup_mics
        cards = [(0, "1-2.6", "USB PnP Sound Device"), (4, "1-1", "USB_MIC")]
        cfg = setup_mics.mono_config(cards, "1-1")
        self.assertEqual(cfg["mono"], {"usb_port": "1-1", "name": "USB_MIC"})
        self.assertEqual(cfg["offset_db"], 0.0)
        self.assertIsNone(setup_mics.mono_config(cards, "1-3"))

    def test_the_same_microphone_twice_fits_one_calibration_with_equal_sides(self):
        pts = [{"reference": r, "left": d, "right": d, "overall": d} for r, d in ((44.0, -50.1), (60.0, -36.8), (68.0, -29.8))]
        fit = calibration.fit(pts, 3.0)
        self.assertEqual(fit["left_offset_db"], fit["right_offset_db"])
        self.assertLessEqual(fit["max_error_db"], 3.0)                      # the real 2026-11-18 probe of the new microphone: within 3 dB


if __name__ == "__main__":
    unittest.main()
