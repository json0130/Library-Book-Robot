"""tools/noise_map.py: the noise log as squares of the floor, and the endpoints that hand them to the operator page."""

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from display.server import allowed
from tools import noise_map

HEADER = ["time_s", "x_m", "y_m", "noise_dB", "left_dB", "right_dB", "balance_dB"]
CAL_HEADER = HEADER + ["sound_dB", "left_sound_dB", "right_sound_dB"]


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class NoiseMapTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_readings_are_put_in_squares_with_median_max_and_count(self):
        csv_path = self.dir / "n.csv"
        write_csv(csv_path, CAL_HEADER, [[1, 0.1, 0.1, -50, -50, -50, 0, 40, 40, 40], [2, 0.2, 0.3, -50, -50, -50, 0, 44, 44, 44],
                                         [3, 0.3, 0.2, -50, -50, -50, 0, 90, 90, 90], [4, 1.2, 0.1, -50, -50, -50, 0, 60, 60, 60]])
        rows, calibrated, skipped = noise_map.read_rows([csv_path])
        grid = noise_map.build(rows, calibrated, cell=0.5)
        self.assertTrue(calibrated)
        self.assertEqual((grid["unit"], grid["calibrated"], grid["readings"]), ("dB", True, 4))
        first = next(c for c in grid["cells"] if c["x"] == 0.25 and c["y"] == 0.25)
        self.assertEqual((first["n"], first["median"], first["max"]), (3, 44.0, 90.0))          # the median, not the mean: one clap does not move it
        self.assertEqual(len(grid["cells"]), 2)
        self.assertEqual((grid["stats"]["min"], grid["stats"]["max"]), (44.0, 60.0))

    def test_without_a_calibration_the_map_says_dbfs_and_not_calibrated(self):
        csv_path = self.dir / "n.csv"
        write_csv(csv_path, HEADER, [[1, 0.1, 0.1, -50, -50, -50, 0], [2, 3.1, 0.1, -40, -40, -40, 0]])
        rows, calibrated, _ = noise_map.read_rows([csv_path])
        grid = noise_map.build(rows, calibrated)
        self.assertEqual((grid["unit"], grid["calibrated"]), ("dBFS", False))

    def test_min_n_leaves_out_thin_squares_and_bad_rows_are_skipped(self):
        csv_path = self.dir / "n.csv"
        write_csv(csv_path, HEADER, [[1, 0.1, 0.1, -50, 0, 0, 0], [2, 0.2, 0.2, -52, 0, 0, 0], [3, 2.0, 2.0, -30, 0, 0, 0], [4, "x", 1, -30, 0, 0, 0],
                                     [5, 1, 1, "nan", 0, 0, 0]])
        rows, calibrated, skipped = noise_map.read_rows([csv_path])
        self.assertEqual((len(rows), skipped), (3, 2))
        self.assertEqual(len(noise_map.build(rows, calibrated, min_n=2)["cells"]), 1)

    def test_several_runs_go_together_and_a_file_that_is_not_a_noise_log_is_refused(self):
        a, b, bad = self.dir / "a.csv", self.dir / "b.csv", self.dir / "bad.csv"
        write_csv(a, HEADER, [[1, 0.1, 0.1, -50, 0, 0, 0]])
        write_csv(b, HEADER, [[1, 0.2, 0.2, -52, 0, 0, 0]])
        write_csv(bad, ["a", "b"], [[1, 2]])
        self.assertEqual(len(noise_map.read_rows([a, b])[0]), 2)
        with self.assertRaises(ValueError):
            noise_map.read_rows([bad])
        with self.assertRaises(ValueError):
            noise_map.build([], False, cell=0.01)

    def test_the_command_writes_the_grid_and_says_when_it_is_not_calibrated(self):
        csv_path, out = self.dir / "n.csv", self.dir / "out" / "grid.json"
        write_csv(csv_path, HEADER, [[1, 0.1, 0.1, -50, 0, 0, 0]])
        said = []
        with unittest_print(said):
            self.assertEqual(noise_map.main([str(csv_path), "--out", str(out)]), 0)
        self.assertTrue(json.loads(out.read_text())["available"])
        self.assertIn("NOT CALIBRATED", " ".join(said))
        write_csv(csv_path, HEADER, [])                                          # a log with no readings yet
        with unittest_print(said):
            self.assertEqual(noise_map.main([str(csv_path), "--out", str(out)]), 1)

    def test_one_microphone_only_and_squares_at_the_floor_are_marked_quiet(self):
        csv_path = self.dir / "n.csv"
        # the right mic is stuck at -48.6 (its floor) everywhere; the left follows the room: quiet at x 0.1, loud at x 3.1
        write_csv(csv_path, HEADER, [[1, 0.1, 0.1, -48, -41, -48.6, 0], [2, 0.2, 0.2, -48, -41.5, -48.6, 0], [3, 3.1, 0.1, -40, -30, -48.6, 0],
                                     [4, 3.2, 0.2, -40, -31, -48.6, 0]])
        left, calibrated, _ = noise_map.read_rows([csv_path], "left")
        self.assertEqual(sorted(v for _, _, v in left), [-41.5, -41, -31, -30])
        right, _, _ = noise_map.read_rows([csv_path], "right")
        self.assertEqual({v for _, _, v in right}, {-48.6})
        grid = noise_map.build(left, calibrated, floor=-39)
        quiet = {c["x"]: c.get("quiet", False) for c in grid["cells"]}
        self.assertEqual(quiet, {0.25: True, 3.25: False})
        self.assertEqual((grid["stats"]["min"], grid["stats"]["max"]), (-30.5, -30.5))        # the colour scale covers only the squares above the floor
        self.assertEqual(grid["stats"]["quietest"], -41.2)
        self.assertEqual(grid["floor"], -39)
        self.assertIsNone(noise_map.build(left, calibrated)["floor"])
        with self.assertRaises(ValueError):
            noise_map.read_rows([csv_path], "middle")

    def test_the_grid_also_carries_fine_points_for_the_smooth_map(self):
        grid = noise_map.build([(0.1, 0.1, 50.0), (0.2, 0.2, 54.0), (0.3, 0.1, 60.0), (2.1, 2.1, 70.0)], True, min_n=1)
        pts = {(p["x"], p["y"]): (p["v"], p["n"]) for p in grid["points"]}
        self.assertEqual(pts, {(0.125, 0.125): (52.0, 2), (0.375, 0.125): (60.0, 1), (2.125, 2.125): (70.0, 1)})
        self.assertEqual(len(noise_map.build([(0.1, 0.1, 50.0)], True, min_n=2)["points"]), 0)

    def test_a_log_from_the_odometry_frame_is_turned_and_shifted_into_the_map_frame_and_can_be_cut_short(self):
        csv_path = self.dir / "n.csv"
        write_csv(csv_path, HEADER, [[1, 1.0, 0.0, -50, -50, -50, 0], [2, 0.0, 1.0, -50, -50, -50, 0], [900, 5.0, 5.0, -50, -50, -50, 0]])
        rows, _, _ = noise_map.read_rows([csv_path], odom_to_map=(90.0, 0.5, -1.0), until_s=100)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0][0], 0.5)           # (1, 0) turned 90 degrees is (0, 1), then shifted by (0.5, -1)
        self.assertAlmostEqual(rows[0][1], 0.0)
        self.assertAlmostEqual(rows[1][0], -0.5)          # (0, 1) turned 90 degrees is (-1, 0)
        self.assertAlmostEqual(rows[1][1], -1.0)
        self.assertEqual(len(noise_map.read_rows([csv_path])[0]), 3)

    def test_a_log_without_the_mic_column_is_refused_for_that_mic_and_the_command_hints_at_the_floor(self):
        csv_path, out = self.dir / "n.csv", self.dir / "grid.json"
        write_csv(csv_path, ["time_s", "x_m", "y_m", "noise_dB"], [[i, 0.1, 0.1, -50 + i] for i in range(12)])
        with self.assertRaises(ValueError):
            noise_map.read_rows([csv_path], "left")
        said = []
        with unittest_print(said):
            self.assertEqual(noise_map.main([str(csv_path), "--out", str(out)]), 2)            # default --mic left: no left_dB column
            self.assertEqual(noise_map.main([str(csv_path), "--out", str(out), "--mic", "both"]), 0)
        self.assertIn("--floor", " ".join(said))
        self.assertEqual(noise_map.floor_hint([(0, 0, v) for v in range(-60, -40)]), -58)
        self.assertIsNone(noise_map.floor_hint([(0, 0, -50)]))

    def test_the_operator_page_may_be_read_from_another_computer_but_not_written(self):
        for path in ("/noise.json", "/survey.json", "/plan.json"):
            self.assertTrue(allowed("GET", path, local=False), path)
            self.assertFalse(allowed("POST", path, local=False), path)


class unittest_print:
    """Collects what noise_map.main prints."""

    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        import builtins
        self.real = builtins.print
        builtins.print = lambda *a, **k: self.sink.append(" ".join(str(x) for x in a))

    def __exit__(self, *exc):
        import builtins
        builtins.print = self.real


if __name__ == "__main__":
    unittest.main()
