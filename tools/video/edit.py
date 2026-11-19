"""The video editor: video/storyboard.yaml -> video/out/stuart_hri2027_sdc.mp4 (HRI 2027 Student Design Competition, max 3 min).

    python tools/video/edit.py                         # the full render (1920x1080, 30 fps, libx264 CRF 20, AAC)
    python tools/video/edit.py --preview               # quick, 480p: check the cut
    python tools/video/edit.py --stills 95,130         # also export those seconds of the result as PNGs (paper figures)
    python tools/video/edit.py --plan                  # print the segments and their timing, render nothing
    python tools/video/edit.py --storyboard other.yaml

Every segment is rendered on its own (picture, badges, labels, captions burned in), the segments are joined without
re-encoding, and the sound is finished in one pass: the clips' own sound (Stuart's voice) ducked under the voiceover, then
loudness-normalised to -16 LUFS. The render fails if the result is longer than `max_seconds` (180).

Storyboard (YAML)
    output: video/out/stuart_hri2027_sdc.mp4
    max_seconds: 180
    recordings: recordings                                  # where the takes are (default: the repo's recordings/)
    voiceover: {wav: video/voiceover.wav, duck_db: 14}      # if the wav is missing, a placeholder is made from each segment's `vo`
    segments:
      - {type: title, duration: 4, title: Stuart, subtitle: "A library robot that reads the room"}
      - type: clip
        src: video/footage/trip_out.mp4      # a file, or take + source: recordings/<take>/<source>.mp4 (face, map, screen, camera)
        in: 2.0                              # seconds, "1:05.5", "event:rfid_read-2s", "event:reply#2+1s", "end-3s"
        out: 20
        speed: 4                             # shows an "x4" label; the clip's sound is dropped when sped up
        zone: common                         # the zone badge (orange "Common area" / blue "Quiet zone")
        operator_driven: true                # a "Operator-driven" badge (the paper says so for teleoperated parts)
        badge: "Simulated: screen recording"  # any other disclosure, as a grey badge
        placeholder: "what to film"          # footage not filmed yet: a "Shot needed" card for `duration` s (in a split: per pane)
        text: "Libraries have quiet zones ..."   # a large line of text over the picture
        captions: [{at: 1.0, dur: 3, text: "Stuart: Hello!"}]   # seconds from the segment's start
        auto_captions: true                  # also caption every word Stuart says, from the take's events.jsonl
        sound_meter: true                    # a dB meter from the take's sound_level events
        audio: keep                          # keep | mute (default: keep at speed 1)
        vo: "the voiceover line said over this segment"
      - type: split                          # two 960x1080 panes with a thin divider
        left:  {take: common1, source: camera, in: "event:greeting-1s", label: "Robot"}
        right: {take: common1, source: face, label: "Face screen"}
        duration: 30                         # else the left pane's length
        audio_from: left                     # left | right | none
        voice: {take: common1, source: audio}   # the sound from film.py's audio.wav (Stuart's speaker), lined up by start times
        captions_from: left
        zone: common
      - {type: stills, duration: 6, images: [a.png, b.png], labels: [Common area, Quiet zone], text: "Same task. Different style."}

Time references
    A number is seconds into the source file; "m:ss" too. "event:NAME" is the time of the first event NAME in the take's
    events.jsonl ("event:NAME#3" the third), "+2s"/"-1.5s" shift it; the take's meta.json (film.py record) or <file>.json
    (sim --record) says when the file started, on the same clock (time.time()). "end" and "end-2s" count from the file's end.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RECORDINGS = ROOT / "recordings"
W, H, FPS = 1920, 1080, 30
BG = "#10161d"
ZONE_COLOURS = {"common": ("#b9591f", "Common area"), "quiet": ("#2f6f9f", "Quiet zone")}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
QUIET_CAPTION = "(shown on screen, no sound)"
SEGMENT_TYPES = ("title", "clip", "split", "stills")


class StoryboardError(ValueError):
    """The storyboard asks for something impossible: says which segment and why."""


# ------------------------------------------------------------------------------------------------- takes and times
@dataclass
class Take:
    """recordings/<name>/: events.jsonl from `sim --film`, meta.json from `film.py record`."""
    name: str
    path: Path
    events: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @classmethod
    def load(cls, name: str, root: Path = RECORDINGS) -> "Take":
        path = root / name
        if not path.is_dir():
            raise StoryboardError(f"take {name!r}: no folder {path}")
        events = []
        ev = path / "events.jsonl"
        if ev.exists():
            for n, line in enumerate(ev.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise StoryboardError(f"{ev}:{n}: not JSON ({exc})") from exc
        meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
        return cls(name, path, events, meta)

    def file(self, source: str) -> Path:
        for candidate in (self.path / source, self.path / f"{source}.mp4", self.path / f"{source}.wav"):
            if candidate.is_file():
                return candidate
        raise StoryboardError(f"take {self.name!r}: no recording {source!r} in {self.path}")

    def start_of(self, source: str) -> Optional[float]:
        """When the source's recording started (time.time()): meta.json from film.py, or <file>.json from `sim --record`."""
        info = (self.meta.get("sources") or {}).get(source) or {}
        if info.get("started"):
            return float(info["started"])
        try:
            side = self.file(source).with_suffix(self.file(source).suffix + ".json")
        except StoryboardError:
            return None
        if side.exists():
            return float(json.loads(side.read_text())["started"])
        return None

    def event_time(self, name: str, nth: int = 1) -> float:
        hits = [e for e in self.events if e.get("type") == name]
        if len(hits) < nth:
            raise StoryboardError(f"take {self.name!r}: {'no' if not hits else 'only ' + str(len(hits))} {name!r} event"
                                  f"{'' if nth == 1 else 's'} (asked for #{nth})")
        return float(hits[nth - 1]["t"])


TIME_RE = re.compile(r"^\s*(?:(?P<min>\d+):)?(?P<sec>\d+(?:\.\d+)?)\s*$")
EVENT_RE = re.compile(r"^\s*event:(?P<name>[a-z_]+)(?:#(?P<nth>\d+))?\s*(?P<shift>[+-]\s*\d+(?:\.\d+)?)?\s*s?\s*$")
END_RE = re.compile(r"^\s*end\s*(?P<shift>[+-]\s*\d+(?:\.\d+)?)?\s*s?\s*$")


def resolve_time(value, take: Optional[Take] = None, source: Optional[str] = None,
                 duration: Optional[float] = None) -> float:
    """Seconds into the source file for a storyboard time (see the module docstring)."""
    if value is None:
        raise StoryboardError("no time given")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise StoryboardError(f"time {value} is negative")
        return float(value)
    text = str(value)
    m = TIME_RE.match(text)
    if m:
        return int(m["min"] or 0) * 60 + float(m["sec"])
    m = END_RE.match(text)
    if m:
        if duration is None:
            raise StoryboardError(f"{text!r}: the file's length is not known")
        return max(0.0, duration + float((m["shift"] or "0").replace(" ", "")))
    m = EVENT_RE.match(text)
    if m:
        if take is None:
            raise StoryboardError(f"{text!r}: event times need a take (take: NAME)")
        start = take.start_of(source) if source else None
        if start is None:
            raise StoryboardError(f"{text!r}: take {take.name!r} has no start time for {source!r} (meta.json or <file>.json)")
        t = take.event_time(m["name"], int(m["nth"] or 1)) - start + float((m["shift"] or "0").replace(" ", ""))
        if t < 0:
            raise StoryboardError(f"{text!r} is {-t:.1f} s before the recording of {source!r} started")
        return t
    raise StoryboardError(f"cannot read the time {value!r} (seconds, m:ss, event:NAME[#n][+-Ns] or end[-Ns])")


# ------------------------------------------------------------------------------------------------- the storyboard
@dataclass
class Pane:
    file: Path
    start: float                       # seconds into the file
    end: float
    speed: float = 1.0
    label: str = ""
    take: Optional[Take] = None
    source: Optional[str] = None
    has_audio: bool = True
    placeholder: str = ""              # not filmed yet: a "Shot needed" panel with this text instead of the footage

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.speed

    def file_time_to_out(self, t_abs: float) -> Optional[float]:
        """A wall-clock event time -> seconds into this pane's output, or None if outside it."""
        if self.take is None or self.source is None:
            return None
        start = self.take.start_of(self.source)
        if start is None:
            return None
        t = t_abs - start
        if not self.start <= t <= self.end:
            return None
        return (t - self.start) / self.speed


@dataclass
class Segment:
    index: int
    kind: str
    duration: float
    spec: dict
    panes: list = field(default_factory=list)
    captions: list = field(default_factory=list)       # (start, end, text) in seconds from the segment's start
    meter: list = field(default_factory=list)          # (time, dB) for the sound meter
    vo: str = ""
    voice: Optional[Pane] = None                        # the sound from another recording (film.py's audio.wav), lined up

    @property
    def name(self) -> str:
        return self.spec.get("id") or f"{self.index:02d}-{self.kind}"


def probe(path: Path) -> dict:
    """Duration and whether there is sound, from ffprobe."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise StoryboardError(f"ffprobe cannot read {path}: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "{}")
    kinds = [s.get("codec_type") for s in data.get("streams", [])]
    return {"duration": float((data.get("format") or {}).get("duration") or 0), "audio": "audio" in kinds, "video": "video" in kinds}


def make_pane(spec: dict, where: str, probe_fn=probe, root: Path = ROOT, takes: Optional[dict] = None,
              recordings: Path = RECORDINGS) -> Pane:
    takes = takes if takes is not None else {}
    take = None
    if spec.get("take"):
        name = spec["take"]
        take = takes.get(name) or takes.setdefault(name, Take.load(name, recordings))
        source = spec.get("source")
        if not source:
            raise StoryboardError(f"{where}: a take needs a source (face, map, camera, screen ...)")
        path = take.file(source)
    elif spec.get("src"):
        path = Path(spec["src"])
        path = path if path.is_absolute() else root / path
        source = None
        if not path.is_file():
            raise StoryboardError(f"{where}: no file {path}")
    else:
        raise StoryboardError(f"{where}: give src: FILE or take: NAME + source: NAME")
    info = probe_fn(path)
    try:
        start = resolve_time(spec.get("in", 0), take, source, info["duration"])
        end = resolve_time(spec.get("out", "end"), take, source, info["duration"])
    except StoryboardError as exc:
        raise StoryboardError(f"{where}: {exc}") from exc
    if info["duration"]:
        end = min(end, info["duration"])
    if end <= start:
        raise StoryboardError(f"{where}: out ({end:.2f} s) is not after in ({start:.2f} s)")
    speed = float(spec.get("speed", 1) or 1)
    if speed <= 0:
        raise StoryboardError(f"{where}: speed must be above 0")
    return Pane(path, start, end, speed, spec.get("label", ""), take, source, info["audio"])


def auto_captions(pane: Pane, quiet: bool) -> list:
    """Every word Stuart says in this pane, from the take's reply events (screen_text in a quiet zone, shown not heard)."""
    if pane.take is None:
        return []
    caps = []
    per_sentence = any(e.get("type") == "speak" for e in pane.take.events)   # timed to the audio, one caption per sentence
    for e in pane.take.events:
        kind = e.get("type")
        if kind == "speak":
            text = f"Stuart: {e.get('text', '')}"
        elif kind == "reply" and e.get("channel") == "speech":
            if per_sentence:
                continue
            text = f"Stuart: {e.get('text', '')}"
        elif (kind == "reply" and e.get("channel") == "screen") or (kind == "screen_text" and quiet):
            text = f"Stuart: {QUIET_CAPTION}"         # read on the screen, not heard: the screen in the video shows the words
        else:
            continue
        t = pane.file_time_to_out(float(e["t"]))
        if t is not None:
            if kind == "speak" and e.get("seconds"):
                length = max(1.2, float(e["seconds"]) / pane.speed + 0.3)
            else:
                length = min(7.0, max(2.0, 0.4 * len(str(e.get("text", "")).split()) + 1.0))
            caps.append([t, t + length, text])
    caps.sort()
    merged = []                                         # the same "(shown on screen...)" twice in a row is one caption
    for c in caps:
        if merged and merged[-1][2] == c[2] and c[0] <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], c[1])
        else:
            if merged and merged[-1][1] > c[0]:
                merged[-1][1] = c[0]                    # one caption at a time
            merged.append(c)
    return [tuple(c) for c in merged]


def simulated_base(take: Optional[Take]) -> bool:
    """The take was filmed with `sim --base fake` (or no base): its sound levels are made up, not measured."""
    start = next((e for e in (take.events if take else []) if e.get("type") == "start"), {})
    return start.get("base", "fake") in ("fake", "sim")


def meter_points(pane: Pane) -> list:
    """The measured sound levels in this pane's time. None from a simulated base: a meter must never show made-up decibels."""
    if pane.take is None or simulated_base(pane.take):
        return []
    out = []
    for e in pane.take.events:
        if e.get("type") == "sound_level" and e.get("db") is not None:
            t = pane.file_time_to_out(float(e["t"]))
            if t is not None:
                out.append((t, float(e["db"]), e.get("unit", "dB")))
    return out


def load_storyboard(path: Path, probe_fn=probe, root: Path = ROOT, takes: Optional[dict] = None) -> tuple:
    """(settings, [Segment]) with every time resolved and every file checked; raises StoryboardError."""
    import yaml
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise StoryboardError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data.get("segments"), list) or not data["segments"]:
        raise StoryboardError(f"{path}: no segments")
    settings = {"output": data.get("output", "video/out/stuart_hri2027_sdc.mp4"), "max_seconds": float(data.get("max_seconds", 180)),
                "voiceover": data.get("voiceover") or {}, "title": data.get("title", "")}
    takes = takes if takes is not None else {}
    rec = Path(data.get("recordings") or RECORDINGS)
    rec = rec if rec.is_absolute() else root / rec
    segments, placeholders, notes = [], [], []
    for i, spec in enumerate(data["segments"], 1):
        if not isinstance(spec, dict):
            raise StoryboardError(f"segment {i}: must be a mapping")
        kind = spec.get("type")
        where = f"segment {i} ({spec.get('id') or kind})"
        if kind not in SEGMENT_TYPES:
            raise StoryboardError(f"{where}: type must be one of {', '.join(SEGMENT_TYPES)}")
        if spec.get("zone") not in (None, "quiet", "common"):
            raise StoryboardError(f"{where}: zone must be quiet or common")
        missing = missing_media(spec, root, rec) if kind in ("clip", "stills") else None
        if missing and spec.get("placeholder"):
            # the footage is not filmed yet: a card saying what is needed, for the planned length, so the cut can be timed now
            if not spec.get("duration"):
                raise StoryboardError(f"{where}: a placeholder needs a duration")
            spec = {**spec, "type": "title", "title": "Shot needed", "subtitle": str(spec["placeholder"]),
                    "footer": f"missing: {missing}", "placeholder_for": kind}
            kind = "title"
            placeholders.append((i, spec.get("id") or where, missing))
        seg = Segment(i, kind, 0.0, spec, vo=str(spec.get("vo") or "").strip())
        if kind in ("title", "stills"):
            seg.duration = float(spec.get("duration", 4))
            if kind == "stills":
                images = spec.get("images") or []
                if not images:
                    raise StoryboardError(f"{where}: stills need images")
                for img in images:
                    p = Path(img) if Path(img).is_absolute() else root / img
                    if not p.is_file():
                        raise StoryboardError(f"{where}: no image {p}")
        elif kind == "clip":
            seg.panes = [make_pane(spec, where, probe_fn, root, takes, rec)]
            seg.duration = float(spec.get("duration") or seg.panes[0].duration)
        else:
            if not isinstance(spec.get("left"), dict) or not isinstance(spec.get("right"), dict):
                raise StoryboardError(f"{where}: a split needs left: and right:")
            seg.panes = []
            for side in ("left", "right"):
                pspec = spec[side]
                gone = missing_media({"type": "clip", **pspec}, root, rec)
                text = pspec.get("placeholder") or spec.get("placeholder")
                if gone and text:
                    if not spec.get("duration"):
                        raise StoryboardError(f"{where}: a split with a placeholder pane needs a duration")
                    seg.panes.append(Pane(Path(""), 0.0, float(spec["duration"]), 1.0, pspec.get("label", ""), placeholder=str(text),
                                          has_audio=False))
                    placeholders.append((i, f"{spec.get('id') or where} {side}", gone))
                else:
                    seg.panes.append(make_pane(pspec, f"{where} {side}", probe_fn, root, takes, rec))
            seg.duration = float(spec.get("duration") or seg.panes[0].duration)
        if seg.duration <= 0:
            raise StoryboardError(f"{where}: duration must be above 0")
        quiet = spec.get("zone") == "quiet"
        for c in spec.get("captions") or []:
            try:
                start, dur, text = float(c["at"]), float(c.get("dur", 3)), str(c["text"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StoryboardError(f"{where}: a caption needs at, text (and dur)") from exc
            seg.captions.append((start, min(seg.duration, start + dur), text))
        if seg.panes and spec.get("auto_captions"):
            src = seg.panes[1] if spec.get("captions_from") == "right" and len(seg.panes) > 1 else seg.panes[0]
            seg.captions += [c for c in auto_captions(src, quiet) if c[0] < seg.duration]
        seg.captions.sort()
        if seg.panes and spec.get("voice"):
            seg.voice = voice_pane(spec["voice"], seg, f"{where} voice", probe_fn, takes, rec)
        if seg.panes and spec.get("sound_meter"):
            for p in seg.panes:
                seg.meter = seg.meter or meter_points(p)
            if not seg.meter:
                notes.append((i, spec.get("id") or where, "no sound meter: the take has no measured sound levels (fake base or none)"))
        segments.append(seg)
    settings["placeholders"], settings["notes"] = placeholders, notes
    return settings, segments


def voice_pane(vspec, seg: Segment, where: str, probe_fn, takes: dict, rec: Path) -> Optional[Pane]:
    """The stretch of a sound recording (film.py's audio.wav: Stuart's speaker) that plays with the first pane's picture, found
    from both recordings' start times. None if the first pane is a placeholder; an error if the times are not known."""
    ref = seg.panes[0]
    if ref.placeholder or not isinstance(vspec, dict) or not vspec.get("take"):
        if isinstance(vspec, dict) and vspec.get("take"):
            return None
        raise StoryboardError(f"{where}: voice needs take: and source:")
    if ref.take is None or ref.speed != 1:
        raise StoryboardError(f"{where}: the first pane must be a take at speed 1 to line the voice up with it")
    name = vspec["take"]
    if not (rec / name).is_dir():
        return None
    take = takes.get(name) or takes.setdefault(name, Take.load(name, rec))
    source = vspec.get("source", "audio")
    try:
        path = take.file(source)
    except StoryboardError:
        return None
    ref_start, voice_start = ref.take.start_of(ref.source), take.start_of(source)
    if ref_start is None or voice_start is None:
        raise StoryboardError(f"{where}: no start time for {ref.source!r} or {source!r} (meta.json)")
    offset = ref_start + ref.start - voice_start
    if offset < 0:
        raise StoryboardError(f"{where}: the sound recording started {-offset:.1f} s after the picture's in point")
    info = probe_fn(path)
    return Pane(path, offset, min(info["duration"] or offset + seg.duration, offset + seg.duration), 1.0, take=take, source=source,
                has_audio=info["audio"])


def missing_media(spec: dict, root: Path, rec: Path) -> Optional[str]:
    """The first file a clip, split or stills segment needs that does not exist yet (None if all are there)."""
    if spec.get("type") == "stills":
        for img in spec.get("images") or []:
            p = Path(img) if Path(img).is_absolute() else root / img
            if not p.is_file():
                return str(img)
        return None
    for pane in ([spec.get("left"), spec.get("right")] if spec.get("type") == "split" else [spec]):
        if not isinstance(pane, dict):
            continue
        if pane.get("src"):
            p = Path(pane["src"])
            if not (p if p.is_absolute() else root / p).is_file():
                return str(pane["src"])
        elif pane.get("take"):
            folder = rec / pane["take"]
            source = pane.get("source") or ""
            if not any((folder / name).is_file() for name in (source, f"{source}.mp4", f"{source}.wav")):
                return f"recordings/{pane['take']}/{source}.mp4"
    return None


def total_duration(segments) -> float:
    return sum(s.duration for s in segments)


# ------------------------------------------------------------------------------------------------- drawing (Pillow)
def font(size: int, bold: bool = False):
    from PIL import ImageFont
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def wrap(draw, text: str, fnt, width: int) -> list:
    lines = []
    for para in str(text).split("\n"):
        words, line = para.split(), ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=fnt) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = w
        lines.append(line)
    return lines


def pill_width(draw, text, fnt) -> int:
    return int(draw.textlength(text, font=fnt) + 2 * pill_pad(fnt)[0])


def pill_pad(fnt) -> tuple:
    """Padding in proportion to the font, so a badge looks the same at 480p and 1080p."""
    return (int(fnt.size * 0.65), int(fnt.size * 0.3))


def pill(draw, xy, text, fnt, fill, ink="#ffffff", pad=None):
    x, y = xy
    pad = pad or pill_pad(fnt)
    w = draw.textlength(text, font=fnt)
    asc, desc = fnt.getmetrics()
    box = (x, y, x + w + 2 * pad[0], y + asc + desc + 2 * pad[1])
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=fill)
    draw.text((x + pad[0], y + pad[1]), text, font=fnt, fill=ink)
    return box


def title_card(path: Path, title: str, subtitle: str = "", footer: str = "", size=(W, H)) -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    k = size[1] / H
    tf, sf, ff = font(int(120 * k), True), font(int(46 * k)), font(int(30 * k))
    lines = wrap(d, title, tf, int(size[0] * 0.85))
    sub = wrap(d, subtitle, sf, int(size[0] * 0.8)) if subtitle else []
    heights = [int(140 * k)] * len(lines) + [int(62 * k)] * len(sub)
    y = (size[1] - sum(heights)) // 2 - int(30 * k)
    for line in lines:
        d.text((size[0] / 2, y), line, font=tf, fill="#f2f5f8", anchor="ma")
        y += int(140 * k)
    y += int(14 * k)
    for line in sub:
        d.text((size[0] / 2, y), line, font=sf, fill="#9fb3c6", anchor="ma")
        y += int(62 * k)
    if footer:
        for i, line in enumerate(reversed(wrap(d, footer, ff, int(size[0] * 0.9)))):
            d.text((size[0] / 2, size[1] - int(70 * k) - i * int(42 * k)), line, font=ff, fill="#7f93a6", anchor="ma")
    img.save(path)
    return path


def overlay_png(path: Path, seg: Segment, size=(W, H)) -> Path:
    """Everything drawn over a clip or split: the zone badge, the speed label, the operator-driven badge, pane labels, the big text."""
    from PIL import Image, ImageDraw
    k = size[1] / H
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    spec = seg.spec
    small, mid, big = font(int(30 * k), True), font(int(34 * k), True), font(int(58 * k), True)
    if spec.get("zone"):
        colour, label = ZONE_COLOURS[spec["zone"]]
        pill(d, (int(36 * k), int(32 * k)), label, mid, colour)
    right = []
    speed = max((p.speed for p in seg.panes), default=1.0)
    if speed != 1:
        right.append((f"x{speed:g}", "#1f2933"))
    if spec.get("operator_driven"):
        right.append(("Operator-driven", "#7a2e8e"))
    if spec.get("badge"):
        right.append((str(spec["badge"]), "#3d4b59"))
    y = int(32 * k)
    for text, colour in right:
        pill(d, (size[0] - int(36 * k) - pill_width(d, text, mid), y), text, mid, colour)
        y += int(70 * k)
    if seg.kind == "split":
        for i, pane in enumerate(seg.panes):
            if pane.label:
                x0 = i * size[0] // 2
                top = int(110 * k) if spec.get("zone") or right else int(30 * k)
                pill(d, (x0 + (size[0] // 2 - pill_width(d, pane.label, small)) // 2, top), pane.label, small, (16, 22, 29, 210))
    elif seg.panes and seg.panes[0].label:
        pill(d, (int(36 * k), size[1] - int(110 * k)), seg.panes[0].label, small, (16, 22, 29, 210))
    if spec.get("text"):
        lines = wrap(d, spec["text"], big, int(size[0] * 0.8))
        lh = int(74 * k)
        top = int(size[1] * 0.40) - len(lines) * lh // 2
        width = max(d.textlength(l, font=big) for l in lines)
        d.rounded_rectangle(((size[0] - width) / 2 - 40 * k, top - 26 * k, (size[0] + width) / 2 + 40 * k, top + len(lines) * lh + 18 * k),
                            radius=int(18 * k), fill=(16, 22, 29, 200))
        for i, line in enumerate(lines):
            d.text((size[0] / 2, top + i * lh), line, font=big, fill="#ffffff", anchor="ma")
    img.save(path)
    return path


def stills_png(path: Path, seg: Segment, root: Path = ROOT, size=(W, H)) -> Path:
    from PIL import Image, ImageDraw
    k = size[1] / H
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    spec = seg.spec
    images = [Image.open(Path(p) if Path(p).is_absolute() else root / p).convert("RGB") for p in spec["images"]]
    labels = list(spec.get("labels") or [])
    n = len(images)
    margin, gap = int(60 * k), int(40 * k)
    top = int(190 * k) if spec.get("text") else int(80 * k)
    bottom = size[1] - (int(200 * k) if spec.get("footer") else int(110 * k))
    cell_w = (size[0] - 2 * margin - (n - 1) * gap) // n
    cell_h = bottom - top
    for i, im in enumerate(images):
        im.thumbnail((cell_w, cell_h))
        x = margin + i * (cell_w + gap) + (cell_w - im.width) // 2
        y = top + (cell_h - im.height) // 2
        img.paste(im, (x, y))
        if i < len(labels) and labels[i]:
            zone = "quiet" if "quiet" in labels[i].lower() else "common" if "common" in labels[i].lower() else None
            fill = ZONE_COLOURS[zone][0] if zone else "#1f2933"
            lf = font(int(34 * k), True)
            pill(d, (int(x + (im.width - pill_width(d, labels[i], lf)) / 2), y + im.height + int(14 * k)), labels[i], lf, fill)
    if spec.get("text"):
        d.text((size[0] / 2, int(60 * k)), spec["text"], font=font(int(76 * k), True), fill="#f2f5f8", anchor="ma")
    if spec.get("footer"):
        for i, line in enumerate(reversed(wrap(d, spec["footer"], font(int(30 * k)), int(size[0] * 0.92)))):
            d.text((size[0] / 2, size[1] - int(60 * k) - i * int(42 * k)), line, font=font(int(30 * k)), fill="#9fb3c6", anchor="ma")
    img.save(path)
    return path


# ------------------------------------------------------------------------------------------------- captions (ASS)
def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def write_ass(path: Path, seg: Segment, size=(W, H)) -> Optional[Path]:
    """The segment's captions (white on a dark box at the bottom) and, when asked, the sound meter (top right)."""
    if not seg.captions and not seg.meter:
        return None
    k = size[1] / H
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {size[0]}", f"PlayResY: {size[1]}", "WrapStyle: 0", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
             "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: Cap,DejaVu Sans,{int(44 * k)},&H00FFFFFF,&H00FFFFFF,&H64101610,&H64101610,0,0,0,0,100,100,0,0,3,{int(14 * k)},0,2,"
             f"{int(160 * k)},{int(160 * k)},{int(56 * k)},1",
             f"Style: Meter,DejaVu Sans,{int(34 * k)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0101610,1,0,0,0,100,100,0,0,3,{int(10 * k)},0,9,"
             f"{int(40 * k)},{int(40 * k)},{int(200 * k)},1",
             f"Style: Bar,DejaVu Sans,10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1", "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for start, end, text in seg.captions:
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{ass_escape(text)}")
    pts = sorted(seg.meter)
    for i, (t, db, unit) in enumerate(pts):
        end = pts[i + 1][0] if i + 1 < len(pts) else seg.duration
        if end <= t:
            continue
        lines.append(f"Dialogue: 1,{ass_time(t)},{ass_time(end)},Meter,,0,0,0,,Sound level  {db:.0f} {unit}")
        frac = max(0.0, min(1.0, (db - 30) / 50 if unit == "dB" else (db + 70) / 60))   # 30..80 dB or -70..-10 dBFS
        x0, y0, bw, bh = int(size[0] - 40 * k - 360 * k), int(262 * k), int(360 * k), int(18 * k)
        colour = "&H00D8A04E&" if frac < 0.4 else "&H0040B8E8&" if frac < 0.7 else "&H003C46E0&"   # BGR: blue, amber, red
        lines.append(f"Dialogue: 1,{ass_time(t)},{ass_time(end)},Bar,,0,0,0,,{{\\pos({x0},{y0})\\1c&H303030&\\bord0\\p1}}"
                     f"m 0 0 l {bw} 0 {bw} {bh} 0 {bh}{{\\p0}}")
        lines.append(f"Dialogue: 2,{ass_time(t)},{ass_time(end)},Bar,,0,0,0,,{{\\pos({x0},{y0})\\1c{colour}\\bord0\\p1}}"
                     f"m 0 0 l {max(1, int(bw * frac))} 0 {max(1, int(bw * frac))} {bh} 0 {bh}{{\\p0}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------- ffmpeg
@dataclass
class Quality:
    size: tuple = (W, H)
    crf: int = 20
    preset: str = "medium"
    threads: int = 3                     # the Jetson has 7.5 GB shared with the robot stack: keep encoders small

    @classmethod
    def preview(cls) -> "Quality":
        return cls((960, 540), 28, "veryfast")         # even halves for the split panes


def run_ffmpeg(args: list, what: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {what}:\n{res.stderr.strip()[-1500:]}\ncommand: {' '.join(cmd)[:800]}")


def encode_args(q: Quality, duration: float) -> list:
    return ["-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", q.preset, "-crf", str(q.crf), "-pix_fmt", "yuv420p",
            "-threads", str(q.threads), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]


def fit_filter(w: int, h: int, label_in: str, label_out: str) -> str:
    """Scale to fit w x h keeping the aspect ratio, pad with the background colour (phone clips are rotated by ffmpeg itself)."""
    return (f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={BG},"
            f"setsar=1,fps={FPS}[{label_out}]")


def pane_video_filter(i: int, pane: Pane, w: int, h: int, duration: float) -> str:
    """Trim, speed, fit, and hold the last frame if the pane is shorter than the segment."""
    chain = (f"[{i}:v]trim=start={pane.start:.3f}:end={pane.end:.3f},setpts=(PTS-STARTPTS)/{pane.speed:g}[t{i}];"
             + fit_filter(w, h, f"t{i}", f"f{i}"))
    pad = max(0.0, duration - pane.duration)
    chain += f";[f{i}]tpad=stop_mode=clone:stop_duration={pad + 0.1:.3f},trim=duration={duration:.3f}[v{i}]"
    return chain


def pane_audio_filter(i: int, pane: Pane, duration: float, keep: bool) -> str:
    if keep and pane.has_audio and pane.speed == 1:
        return (f"[{i}:a]atrim=start={pane.start:.3f}:end={pane.end:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=channel_layouts=stereo,apad=whole_dur={duration:.3f}[a]")
    return f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.3f}[a]"


def subtitle_filter(ass: Optional[Path], label_in: str, label_out: str) -> str:
    if ass is None:
        return f"[{label_in}]null[{label_out}]"
    escaped = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"[{label_in}]ass='{escaped}':fontsdir=/usr/share/fonts/truetype/dejavu[{label_out}]"


def render_segment(seg: Segment, out: Path, work: Path, q: Quality, root: Path = ROOT) -> Path:
    w, h = q.size
    if seg.kind in ("title", "stills"):
        png = work / f"{seg.name}.png"
        if seg.kind == "title":
            title_card(png, seg.spec.get("title", ""), seg.spec.get("subtitle", ""), seg.spec.get("footer", ""), q.size)
        else:
            stills_png(png, seg, root, q.size)
        ass = write_ass(work / f"{seg.name}.ass", seg, q.size)
        graph = subtitle_filter(ass, "0:v", "v")
        run_ffmpeg(["-loop", "1", "-framerate", str(FPS), "-i", str(png), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-filter_complex", graph, "-map", "[v]", "-map", "1:a", *encode_args(q, seg.duration), str(out)], seg.name)
        return out
    keep = seg.spec.get("audio", "keep") != "mute"
    inputs = []
    pane_size = (w, h) if seg.kind == "clip" else (w // 2, h)
    for n_pane, p in enumerate(seg.panes):
        if p.placeholder:
            card = title_card(work / f"{seg.name}-pane{n_pane}.png", "Shot needed", p.placeholder, size=pane_size)
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{seg.duration + 0.5:.3f}", "-i", str(card)]
        else:
            inputs += ["-i", str(p.file)]
    overlay = overlay_png(work / f"{seg.name}-over.png", seg, q.size)
    n = len(seg.panes)
    inputs += ["-i", str(overlay)]
    if seg.voice is not None:
        inputs += ["-i", str(seg.voice.file)]
    ass = write_ass(work / f"{seg.name}.ass", seg, q.size)
    if seg.kind == "clip":
        graph = pane_video_filter(0, seg.panes[0], w, h, seg.duration)
        graph += f";[v0][{n}:v]overlay=0:0[o]"
        audio = pane_audio_filter(0, seg.panes[0], seg.duration, keep)
    else:
        pw = w // 2
        graph = pane_video_filter(0, seg.panes[0], pw, h, seg.duration) + ";" + pane_video_filter(1, seg.panes[1], pw, h, seg.duration)
        graph += (f";[v0][v1]hstack=inputs=2,drawbox=x={pw - 2}:y=0:w=4:h={h}:color=0x3a4652:t=fill[s]"
                  f";[s][{n}:v]overlay=0:0[o]")
        side = {"left": 0, "right": 1}.get(seg.spec.get("audio_from", "left"))
        audio = pane_audio_filter(side, seg.panes[side], seg.duration, keep) if side is not None else \
            f"anullsrc=r=48000:cl=stereo,atrim=duration={seg.duration:.3f}[a]"
    if seg.voice is not None and keep:
        audio = pane_audio_filter(n + 1, seg.voice, seg.duration, True)
    graph += ";" + subtitle_filter(ass, "o", "v") + ";" + audio
    run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *encode_args(q, seg.duration), str(out)], seg.name)
    return out


def concat(parts: list, out: Path, work: Path) -> Path:
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)], "joining the segments")
    return out


# ------------------------------------------------------------------------------------------------- voiceover
def silence_wav(path: Path, seconds: float, rate: int = 48000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(seconds * rate))
    return path


def tts_line(text: str, path: Path) -> bool:
    """One voiceover line with the robot's TTS (Kokoro on the AI server, services/speech.py). False if it is not reachable."""
    try:
        from robot.config import load_config
        from robot.services.speech import Speech
        speech = Speech.from_config(load_config())
        if speech.check(timeout=2.0):
            return False
        speech.voice = speech.voice or "af_heart"
        path.write_bytes(speech.synthesize(text))
        return True
    except Exception:
        return False


def placeholder_voiceover(segments, path: Path, work: Path, total: float) -> tuple:
    """Each segment's `vo` line spoken at the segment's start (TTS if reachable, else silence). (path, how)."""
    starts, t = [], 0.0
    for s in segments:
        starts.append(t)
        t += s.duration
    pieces = []
    for s, start in zip(segments, starts):
        if s.vo:
            wav = work / f"vo-{s.index:02d}.wav"
            if tts_line(s.vo, wav):
                pieces.append((start + 0.3, wav))
    if not pieces:
        return silence_wav(path, total), "silent (no TTS reachable: record video/voiceover.wav, or start the AI server)"
    inputs, labels, graph = [], [], []
    for i, (start, wav) in enumerate(pieces):
        inputs += ["-i", str(wav)]
        ms = int(start * 1000)
        graph.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,adelay={ms}|{ms}[p{i}]")
        labels.append(f"[p{i}]")
    graph.append("".join(labels) + f"amix=inputs={len(pieces)}:normalize=0,apad=whole_dur={total:.3f}[vo]")
    run_ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", "[vo]", "-t", f"{total:.3f}", str(path)], "the placeholder voiceover")
    return path, f"placeholder TTS, {len(pieces)} line(s)"


def finish_audio(video: Path, voiceover: Optional[Path], out: Path, duck_db: float = 14.0) -> Path:
    """Duck the clips' sound under the voiceover, mix, loudness-normalise to -16 LUFS; the picture is copied as it is."""
    if voiceover is None:
        graph = "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]"
        inputs = ["-i", str(video)]
    else:
        ratio = max(2.0, 10 ** (duck_db / 20))
        graph = ("[1:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[vo][key];"
                 f"[0:a][key]sidechaincompress=threshold=0.02:ratio={ratio:.1f}:attack=30:release=400[duck];"
                 "[duck][vo]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
        inputs = ["-i", str(video), "-i", str(voiceover)]
    run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(out)], "the sound")
    return out


# ------------------------------------------------------------------------------------------------- the whole render
def export_stills(video: Path, times, out_dir: Path) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for t in times:
        png = out_dir / f"still_{t:06.2f}s.png"
        run_ffmpeg(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(png)], f"the still at {t} s")
        made.append(png)
    return made


def print_plan(segments, out=sys.stdout) -> None:
    t = 0.0
    for s in segments:
        what = ", ".join(f"{p.file.name} {p.start:.1f}-{p.end:.1f}s" + (f" x{p.speed:g}" if p.speed != 1 else "")
                         for p in s.panes) or s.spec.get("title") or s.spec.get("text") or ""
        print(f"  {int(t // 60)}:{t % 60:05.2f}  {s.duration:6.2f}s  {s.name:22} {what}"
              + (f"  [{len(s.captions)} caption(s)]" if s.captions else ""), file=out)
        t += s.duration
    print(f"  total {int(t // 60)}:{t % 60:05.2f} ({t:.1f} s)", file=out)


def render(storyboard: Path, preview: bool = False, stills=(), out=sys.stdout, root: Path = ROOT, keep_work: bool = False) -> Path:
    settings, segments = load_storyboard(storyboard, root=root)
    total = total_duration(segments)
    print_plan(segments, out)
    for i, name, missing in settings.get("placeholders", []):
        print(f"  PLACEHOLDER segment {i} ({name}): {missing} is not filmed yet", file=out)
    for i, name, note in settings.get("notes", []):
        print(f"  NOTE segment {i} ({name}): {note}", file=out)
    if total > settings["max_seconds"]:
        raise StoryboardError(f"the storyboard is {total:.1f} s long, over the limit of {settings['max_seconds']:g} s: cut something")
    q = Quality.preview() if preview else Quality()
    final = Path(settings["output"])
    final = final if final.is_absolute() else root / final
    if preview:
        final = final.with_name(final.stem + "_preview" + final.suffix)
    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="edit-", dir=final.parent))
    t0 = time.time()
    try:
        parts = []
        for s in segments:
            part = work / f"{s.index:02d}.mp4"
            print(f"  rendering {s.name} ({s.duration:.1f} s)...", file=out, flush=True)
            render_segment(s, part, work, q, root)
            parts.append(part)
        joined = concat(parts, work / "joined.mp4", work)
        vo_cfg = settings["voiceover"]
        vo_path = Path(vo_cfg.get("wav", "video/voiceover.wav"))
        vo_path = vo_path if vo_path.is_absolute() else root / vo_path
        if vo_path.is_file():
            voiceover, how = vo_path, f"recorded ({vo_path.name})"
        elif any(s.vo for s in segments):
            voiceover, how = placeholder_voiceover(segments, work / "voiceover_placeholder.wav", work, total)
        else:
            voiceover, how = None, "none"
        print(f"  voiceover: {how}", file=out)
        finish_audio(joined, voiceover, final, float(vo_cfg.get("duck_db", 14)))
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    info = probe(final)
    size_mb = final.stat().st_size / 1e6
    print(f"wrote {final}: {info['duration']:.1f} s, {size_mb:.1f} MB, in {time.time() - t0:.0f} s"
          + ("" if size_mb < 100 else "  (over 100 MB: raise the CRF)"), file=out)
    if info["duration"] > settings["max_seconds"] + 0.5:
        raise StoryboardError(f"the result is {info['duration']:.1f} s, over {settings['max_seconds']:g} s")
    if stills:
        for png in export_stills(final, stills, final.parent / "stills"):
            print(f"  still: {png}", file=out)
    return final


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="edit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--storyboard", default=str(ROOT / "video" / "storyboard.yaml"))
    p.add_argument("--preview", action="store_true", help="fast 480p render")
    p.add_argument("--stills", default="", help="comma-separated seconds of the result to export as PNGs")
    p.add_argument("--plan", action="store_true", help="only print the segments and their timing")
    p.add_argument("--keep-work", action="store_true", help="keep the rendered segments (for debugging)")
    args = p.parse_args(argv)
    try:
        if args.plan:
            settings, segments = load_storyboard(Path(args.storyboard))
            print_plan(segments)
            for i, name, missing in settings.get("placeholders", []):
                print(f"  PLACEHOLDER segment {i} ({name}): {missing} is not filmed yet")
            for i, name, note in settings.get("notes", []):
                print(f"  NOTE segment {i} ({name}): {note}")
            over = total_duration(segments) - settings["max_seconds"]
            return 1 if over > 0 else 0
        stills = [float(s) for s in args.stills.split(",") if s.strip()]
        render(Path(args.storyboard), args.preview, stills, keep_work=args.keep_work)
        return 0
    except (StoryboardError, RuntimeError) as exc:
        print(f"edit.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
