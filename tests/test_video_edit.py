"""tools/video/edit.py: the storyboard, the times relative to events, captions, and (with ffmpeg) a whole synthetic render."""

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.video import edit
from tools.video.edit import QUIET_CAPTION, StoryboardError, Take, load_storyboard, resolve_time

START = 1_800_000_000.0


def fake_probe(duration=20.0, audio=True):
    return lambda path: {"duration": duration, "audio": audio, "video": True}


def write_take(root: Path, name="t1", zone="common", camera_offset=0.5) -> Path:
    take = root / name
    take.mkdir(parents=True)
    (take / "camera.mp4").write_bytes(b"")
    (take / "face.mp4").write_bytes(b"")
    (take / "camera.mp4.json").write_text(json.dumps({"started": START + camera_offset}))
    (take / "meta.json").write_text(json.dumps({"sources": {"face": {"started": START, "file": "face.mp4"}}}))
    speech = zone == "common"
    events = [{"t": START + 1.5, "type": "greeting"},
              {"t": START + 2.0, "type": "reply", "text": "Hello! My name is Stuart.", "channel": "speech" if speech else "screen"},
              {"t": START + 4.0, "type": "rfid_read", "title": "Cosmos"},
              {"t": START + 6.0, "type": "reply", "text": "Thanks! Cosmos is returned.", "channel": "speech" if speech else "screen"},
              {"t": START + 6.0, "type": "screen_text", "text": "Cosmos is returned."},
              {"t": START + 9.0, "type": "rfid_read", "title": "Dune"},
              {"t": START + 3.0, "type": "sound_level", "db": 38.4, "unit": "dB"},
              {"t": START + 3.5, "type": "sound_level", "db": 39.0, "unit": "dB"}]
    (take / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return take


class ResolveTimeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        write_take(self.dir)
        self.take = Take.load("t1", self.dir)

    def test_plain_times(self):
        self.assertEqual(resolve_time(12), 12.0)
        self.assertEqual(resolve_time("12.5"), 12.5)
        self.assertEqual(resolve_time("1:05.5"), 65.5)
        self.assertEqual(resolve_time("end", duration=30), 30.0)
        self.assertEqual(resolve_time("end-2s", duration=30), 28.0)
        self.assertEqual(resolve_time("end - 2.5", duration=30), 27.5)

    def test_an_event_time_is_seconds_into_that_recording(self):
        self.assertEqual(resolve_time("event:rfid_read", self.take, "face"), 4.0)                # face started at START
        self.assertEqual(resolve_time("event:rfid_read", self.take, "camera"), 3.5)              # camera started 0.5 s later
        self.assertEqual(resolve_time("event:rfid_read-2s", self.take, "face"), 2.0)
        self.assertEqual(resolve_time("event:rfid_read+1.5s", self.take, "face"), 5.5)
        self.assertEqual(resolve_time("event:rfid_read#2", self.take, "face"), 9.0)
        self.assertEqual(resolve_time("event:rfid_read#2 - 1", self.take, "face"), 8.0)

    def test_what_cannot_be_resolved_says_why(self):
        for value, take, source, words in (("event:rfid_read#3", self.take, "face", "only 2"),
                                           ("event:unload", self.take, "face", "no 'unload'"),
                                           ("event:greeting-5s", self.take, "face", "before the recording"),
                                           ("event:greeting", None, None, "need a take"),
                                           ("event:greeting", self.take, "map", "no start time"),
                                           ("end", None, None, "length"),
                                           ("soon", None, None, "cannot read"),
                                           (-1, None, None, "negative")):
            with self.subTest(value=value), self.assertRaises(StoryboardError) as cm:
                resolve_time(value, take, source)
            self.assertIn(words, str(cm.exception))


class StoryboardTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        write_take(self.dir / "rec", "common1", "common")
        write_take(self.dir / "rec", "quiet1", "quiet")
        (self.dir / "phone.mp4").write_bytes(b"")
        (self.dir / "a.png").write_bytes(b"")

    def board(self, body: str, probe=None):
        path = self.dir / "board.yaml"
        path.write_text(f"recordings: {self.dir / 'rec'}\n" + textwrap.dedent(body))
        return load_storyboard(path, probe_fn=probe or fake_probe(), root=self.dir)

    def test_every_segment_type_and_its_length(self):
        settings, segs = self.board("""
            segments:
              - {type: title, duration: 3, title: Stuart}
              - {type: clip, src: phone.mp4, in: 2, out: 14, speed: 4}
              - type: split
                left: {take: common1, source: camera, in: "event:greeting-1s", out: "event:rfid_read+2s", label: Robot}
                right: {take: common1, source: face, in: "event:greeting-1s", label: Face}
              - {type: stills, duration: 5, images: [a.png, a.png], labels: [Common area, Quiet zone]}
        """)
        self.assertEqual([s.kind for s in segs], ["title", "clip", "split", "stills"])
        self.assertEqual(segs[1].duration, 3.0)                                   # 12 s at x4
        left = segs[2].panes[0]
        self.assertEqual((left.start, left.end), (0.0, 5.5))                      # camera: greeting 1.0, rfid 3.5
        self.assertEqual(segs[2].duration, 5.5)                                   # the left pane's length
        self.assertEqual(edit.total_duration(segs), 16.5)
        self.assertEqual(settings["max_seconds"], 180)

    def test_auto_captions_caption_every_spoken_line_in_the_common_area(self):
        _, segs = self.board("""
            segments:
              - {type: clip, take: common1, source: face, in: 1, out: 8, auto_captions: true, zone: common}
        """)
        texts = [c[2] for c in segs[0].captions]
        self.assertEqual(texts, ["Stuart: Hello! My name is Stuart.", "Stuart: Thanks! Cosmos is returned."])
        self.assertEqual(segs[0].captions[0][0], 1.0)                             # reply at 2.0 s, the clip starts at 1.0 s

    def test_quiet_zone_answers_are_captioned_as_shown_not_heard(self):
        _, segs = self.board("""
            segments:
              - {type: clip, take: quiet1, source: face, in: 1, out: 8, auto_captions: true, zone: quiet, sound_meter: true}
        """)
        texts = [c[2] for c in segs[0].captions]
        self.assertEqual(texts, [f"Stuart: {QUIET_CAPTION}"] * 2)                 # the 6.0 s reply and screen_text are one caption
        self.assertEqual([round(t, 1) for t, _, _ in segs[0].meter], [2.0, 2.5])
        self.assertNotIn("Cosmos", " ".join(texts))

    def test_speed_shifts_event_captions(self):
        _, segs = self.board("""
            segments:
              - {type: clip, take: common1, source: face, in: 0, out: 8, speed: 2, auto_captions: true}
        """)
        self.assertEqual([c[0] for c in segs[0].captions], [1.0, 3.0])

    def test_hand_written_captions_are_kept_and_clipped_to_the_segment(self):
        _, segs = self.board("""
            segments:
              - {type: clip, src: phone.mp4, in: 0, out: 4, captions: [{at: 3, dur: 5, text: "Stuart: Hi"}]}
        """)
        self.assertEqual(segs[0].captions, [(3.0, 4.0, "Stuart: Hi")])

    def test_mistakes_are_reported_with_the_segment(self):
        cases = {
            "segments: []": "no segments",
            "segments:\n  - {type: movie}": "type must be",
            "segments:\n  - {type: clip}": "src: FILE",
            "segments:\n  - {type: clip, src: nothere.mp4}": "no file",
            "segments:\n  - {type: clip, src: phone.mp4, in: 5, out: 2}": "not after",
            "segments:\n  - {type: clip, src: phone.mp4, zone: loud}": "quiet or common",
            "segments:\n  - {type: clip, take: nope, source: face}": "no folder",
            "segments:\n  - {type: clip, take: common1}": "needs a source",
            "segments:\n  - {type: clip, take: common1, source: face, in: 'event:unload'}": "segment 1",
            "segments:\n  - {type: split, left: {src: phone.mp4}}": "left: and right:",
            "segments:\n  - {type: stills, images: [missing.png]}": "no image",
            "segments:\n  - {type: clip, src: phone.mp4, captions: [{text: hi}]}": "at, text",
        }
        for body, words in cases.items():
            with self.subTest(body=body), self.assertRaises(StoryboardError) as cm:
                self.board(body)
            self.assertIn(words, str(cm.exception))

    def test_a_storyboard_over_three_minutes_is_refused_before_rendering(self):
        path = self.dir / "long.yaml"
        path.write_text("segments:\n" + "".join("  - {type: title, duration: 50, title: x}\n" for _ in range(4)))
        with self.assertRaises(StoryboardError) as cm:
            edit.render(path, root=self.dir)
        self.assertIn("over the limit", str(cm.exception))


class AssTest(unittest.TestCase):
    def test_times_and_escaping(self):
        self.assertEqual(edit.ass_time(65.432), "0:01:05.43")
        self.assertEqual(edit.ass_escape("a {b}\nc"), "a (b)\\Nc")

    def test_captions_and_meter_are_written(self):
        seg = edit.Segment(1, "clip", 10.0, {}, captions=[(1.0, 3.0, "Stuart: Hello")], meter=[(2.0, 38.0, "dB"), (4.0, 70.0, "dB")])
        with tempfile.TemporaryDirectory() as d:
            text = edit.write_ass(Path(d) / "s.ass", seg).read_text()
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.00,Cap,,0,0,0,,Stuart: Hello", text)
        self.assertIn("Sound level  38 dB", text)
        self.assertIn("Sound level  70 dB", text)
        self.assertIsNone(edit.write_ass(Path("/nonexistent/x.ass"), edit.Segment(1, "clip", 1.0, {})))


def ffmpeg_ready() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return " ass " in out and "sidechaincompress" in out and "libx264" in enc


@unittest.skipUnless(ffmpeg_ready(), "needs Ubuntu's ffmpeg (libx264, ass, sidechaincompress)")
class SyntheticRenderTest(unittest.TestCase):
    def test_the_whole_pipeline_on_test_patterns(self):
        from tools.video.synthetic import make_all
        with tempfile.TemporaryDirectory() as d:
            board = make_all(Path(d))
            out = edit.render(board, preview=True, stills=[1.0], out=open("/dev/null", "w"))
            info = edit.probe(out)
            self.assertAlmostEqual(info["duration"], 23.0, delta=0.3)
            self.assertTrue(info["audio"])
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0",
                                      str(out)], capture_output=True, text=True).stdout
            self.assertIn("960,540,30/1", streams)
            self.assertTrue((out.parent / "stills" / "still_001.00s.png").exists())


if __name__ == "__main__":
    unittest.main()
