"""Noise calibration: from the microphones' dBFS to a sound level in dB.

noise_logger.py publishes the level of each USB microphone in dBFS (decibels below the microphone's full scale, always
negative). A phone sound meter shows dB SPL. One fixed offset per microphone turns one into the other:

    dB = dBFS + offset          (the combined level is the average power of the two microphones, as in noise_logger.py)

calibrate_noise.py measures three sound levels against a phone, fits the offsets and saves ~/noise_calibration.json. The
robot_link_api, the noise logger and the noise map read it; without it the API reports `unit: "dBFS", calibrated: false`.
"""

import json
import math
import os
import time
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.expanduser("~/noise_calibration.json")
MAX_OFFSET_DB = 150.0
FILE_VERSION = 1


class CalibrationError(ValueError):
    """The calibration file is missing a field or has nonsense in it."""


class Calibration:
    def __init__(self, left_offset_db: float, right_offset_db: float, level_mode: str = "peak", created: str = "",
                 max_error_db: Optional[float] = None, mixer: Optional[dict] = None):
        for name, v in (("left_offset_db", left_offset_db), ("right_offset_db", right_offset_db)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or abs(v) > MAX_OFFSET_DB:
                raise CalibrationError("%s must be a number within +-%g, not %r" % (name, MAX_OFFSET_DB, v))
        self.left_offset_db, self.right_offset_db = float(left_offset_db), float(right_offset_db)
        self.level_mode, self.created, self.max_error_db, self.mixer = level_mode, created, max_error_db, mixer or {}

    def apply(self, left_dbfs: float, right_dbfs: float) -> Dict[str, float]:
        """{overall, left, right, balance} in dB from the two microphones' dBFS."""
        left, right = left_dbfs + self.left_offset_db, right_dbfs + self.right_offset_db
        return {"overall": round(power_mean_db(left, right), 1), "left": round(left, 1), "right": round(right, 1),
                "balance": round(left - right, 1)}


def power_mean_db(*levels_db: float) -> float:
    """The level of the average power of several levels in dB (what noise_logger.py calls overall)."""
    power = sum(10 ** (level / 10.0) for level in levels_db) / len(levels_db)
    return 10 * math.log10(power) if power > 0 else -96.0


def load(path: str = DEFAULT_PATH) -> Optional[Calibration]:
    """The saved calibration, or None if there is no file. Raises CalibrationError for a file that is there but wrong."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CalibrationError("%s cannot be read: %s" % (path, exc))
    if not isinstance(data, dict):
        raise CalibrationError("%s is not a JSON object" % path)
    try:
        return Calibration(data["left_offset_db"], data["right_offset_db"], str(data.get("level_mode", "peak")),
                           str(data.get("created", "")), data.get("max_error_db"), data.get("mixer"))
    except KeyError as exc:
        raise CalibrationError("%s has no %s" % (path, exc))


def fit(points: List[dict], tolerance_db: float = 3.0) -> dict:
    """Fit one offset per microphone to measurements against a reference sound meter.

    Each point: {"reference": dB on the meter, "left": dBFS, "right": dBFS, "overall": dBFS} (medians over a few seconds).
    offset = mean(reference - dBFS) over the points, i.e. a line of slope 1 (a microphone's response in dB is a straight
    line of slope 1 as long as its gain is fixed and it does not clip). The slope that the points really have is reported too:
    far from 1 means automatic gain control, clipping or a meter that weights frequencies (A-weighting) differently.
    """
    if len(points) < 3:
        raise ValueError("three levels are needed, got %d" % len(points))
    refs = [p["reference"] for p in points]
    if max(refs) - min(refs) < 10:
        raise ValueError("the levels are too close together (%.1f dB apart): use a quiet room, normal talking and something loud"
                         % (max(refs) - min(refs)))
    out: Dict[str, object] = {}
    for mic in ("left", "right"):
        offset = sum(p["reference"] - p[mic] for p in points) / len(points)
        out[mic + "_offset_db"] = round(offset, 2)
        out[mic + "_slope"] = round(slope([p[mic] for p in points], refs), 2)
    cal = Calibration(out["left_offset_db"], out["right_offset_db"])
    rows = []
    for p in points:
        combined = cal.apply(p["left"], p["right"])
        rows.append({"reference": p["reference"], "overall": combined["overall"], "error_db": round(combined["overall"] - p["reference"], 1),
                     "left_error_db": round(p["left"] + cal.left_offset_db - p["reference"], 1),
                     "right_error_db": round(p["right"] + cal.right_offset_db - p["reference"], 1)})
    errors = [abs(e) for r in rows for e in (r["error_db"], r["left_error_db"], r["right_error_db"])]
    out["points"] = rows
    out["max_error_db"] = round(max(errors), 1)
    out["rms_error_db"] = round(math.sqrt(sum(e * e for e in errors) / len(errors)), 1)
    warnings = []
    if out["max_error_db"] > tolerance_db:
        warnings.append("the largest error is %.1f dB (more than %.1f): the microphones do not follow the meter in a straight "
                        "line. Check automatic gain control and the capture volume, and measure again." % (out["max_error_db"], tolerance_db))
    for mic in ("left", "right"):
        if not 0.85 <= out[mic + "_slope"] <= 1.15:
            warnings.append("the %s microphone's response has a slope of %.2f (it should be about 1.0)." % (mic, out[mic + "_slope"]))
    out["warnings"] = warnings
    return out


def slope(xs: List[float], ys: List[float]) -> float:
    """Least-squares slope of y against x."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else float("nan")


def save(path: str, result: dict, level_mode: str = "peak", mixer: Optional[dict] = None, reference: str = "phone sound meter") -> None:
    """Write the fit made by fit() as the calibration file (the old file is kept as <path>.bak)."""
    Calibration(result["left_offset_db"], result["right_offset_db"])           # refuses nonsense
    body = {"version": FILE_VERSION, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "reference": reference,
            "level_mode": level_mode, "left_offset_db": result["left_offset_db"], "right_offset_db": result["right_offset_db"],
            "max_error_db": result["max_error_db"], "rms_error_db": result["rms_error_db"], "points": result["points"],
            "mixer": mixer or {}}
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class Reloader:
    """Keeps the calibration up to date: loads the file again when it changes (calibrate_noise.py writes it while the robot runs)."""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self.calibration: Optional[Calibration] = None
        self.error = ""
        self._stamp: object = "unset"

    def poll(self) -> Optional[Calibration]:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self.error = ""
            try:
                self.calibration = load(self.path)
            except CalibrationError as exc:
                self.calibration, self.error = None, str(exc)
        return self.calibration
