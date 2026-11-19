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


def write_take(root: Path, name="t1", zone="common", camera_offset=0.5, base="turtlebot") -> Path:
    take = root / name
    take.mkdir(parents=True)
    (take / "camera.mp4").write_bytes(b"")
    (take / "face.mp4").write_bytes(b"")
    (take / "camera.mp4.json").write_text(json.dumps({"started": START + camera_offset}))
    (take / "meta.json").write_text(json.dumps({"sources": {"face": {"started": START, "file": "face.mp4"}}}))
    speech = zone == "common"
    events = [{"t": START, "type": "start", "base": base},
              {"t": START + 1.5, "type": "greeting"},
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

    def test_spoken_sentences_are_captioned_when_they_play(self):
        take = self.dir / "rec" / "common1"
        with (take / "events.jsonl").open("a") as f:
            f.write(json.dumps({"t": START + 2.2, "type": "speak", "text": "Hello!", "seconds": 0.8}) + "\n")
            f.write(json.dumps({"t": START + 3.1, "type": "speak", "text": "My name is Stuart.", "seconds": 1.5}) + "\n")
        _, segs = self.board("""
            segments:
              - {type: clip, take: common1, source: face, in: 2, out: 5, auto_captions: true}
        """)
        self.assertEqual([(round(a, 2), round(b, 2), t) for a, b, t in segs[0].captions],
                         [(0.2, 1.1, "Stuart: Hello!"), (1.1, 2.9, "Stuart: My name is Stuart.")])   # replies are not captioned twice

    def test_what_the_visitor_says_is_captioned_too(self):
        take = self.dir / "rec" / "common1"
        with (take / "events.jsonl").open("a") as f:
            f.write(json.dumps({"t": START + 3.0, "type": "speech_in", "text": "can I return this book?"}) + "\n")
        _, segs = self.board("""
            segments:
              - {type: clip, take: common1, source: face, in: 2.5, out: 5, auto_captions: true}
        """)
        self.assertIn((0.5, "Visitor: can I return this book?"), [(round(a, 2), t) for a, _, t in segs[0].captions])

    def test_quiet_zone_answers_are_captioned_as_shown_not_heard(self):
        _, segs = self.board("""
            segments:
              - {type: clip, take: quiet1, source: face, in: 1, out: 8, auto_captions: true, zone: quiet, sound_meter: true}
        """)
        texts = [c[2] for c in segs[0].captions]
        self.assertEqual(texts, [f"Stuart: {QUIET_CAPTION}"] * 2)                 # the 6.0 s reply and screen_text are one caption
        self.assertEqual([round(t, 1) for t, _, _ in segs[0].meter], [2.0, 2.5])
        self.assertNotIn("Cosmos", " ".join(texts))

    def test_a_simulated_base_never_shows_a_sound_meter(self):
        write_take(self.dir / "rec", "quietsim", "quiet", base="fake")
        settings, segs = self.board("""
            segments:
              - {type: clip, take: quietsim, source: face, in: 1, out: 8, zone: quiet, sound_meter: true}
        """)
        self.assertEqual(segs[0].meter, [])
        self.assertIn("no measured sound levels", settings["notes"][0][2])

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

    def test_footage_not_filmed_yet_becomes_a_placeholder_card_of_the_planned_length(self):
        settings, segs = self.board("""
            segments:
              - {type: clip, src: notyet.mp4, duration: 12, placeholder: "Quiet study room, students working", zone: quiet}
              - type: split
                duration: 20
                placeholder: "Robot driving + live map"
                left: {take: trip1, source: camera}
                right: {take: common1, source: face}
        """)
        self.assertEqual([(s.kind, s.duration) for s in segs], [("title", 12.0), ("split", 20.0)])
        self.assertEqual(segs[0].spec["title"], "Shot needed")
        self.assertIn("Quiet study room", segs[0].spec["subtitle"])
        left, right = segs[1].panes
        self.assertEqual((left.placeholder, right.placeholder), ("Robot driving + live map", ""))   # only the missing pane
        self.assertEqual(right.file.name, "face.mp4")
        self.assertEqual([m for _, _, m in settings["placeholders"]], ["notyet.mp4", "recordings/trip1/camera.mp4"])
        with self.assertRaises(StoryboardError):                                  # a placeholder still needs its length
            self.board("segments:\n  - {type: clip, src: notyet.mp4, placeholder: later}")

    def test_photo_segments(self):
        (self.dir / "p.jpg").write_bytes(b"")
        _, segs = self.board("""
            segments:
              - {type: photo, image: p.jpg, duration: 5, fit: contain, label: A, credit: B, zone: quiet}
        """)
        self.assertEqual((segs[0].kind, segs[0].duration), ("photo", 5.0))
        with self.assertRaises(StoryboardError) as cm:
            self.board("segments:\n  - {type: photo, duration: 3}")
        self.assertIn("image:", str(cm.exception))
        with self.assertRaises(StoryboardError) as cm:
            self.board("segments:\n  - {type: photo, image: nothere.jpg, duration: 3}")
        self.assertIn("no image", str(cm.exception))
        settings, segs = self.board("segments:\n  - {type: photo, image: nothere.jpg, duration: 3, placeholder: a photo of a library}")
        self.assertEqual((segs[0].kind, settings["placeholders"][0][2]), ("title", "nothere.jpg"))        # a card, not an error

    def test_inset_pop_up_on_a_clip(self):
        settings, segs = self.board("""
            segments:
              - {type: clip, src: phone.mp4, in: 0, out: 20, duration: 12, inset: {src: phone.mp4, in: 0, out: 20, speed: 4, at: 3, label: CAD}}
        """)
        seg = segs[0]
        self.assertEqual((seg.inset.duration, seg.inset_at), (5.0, 3.0))
        self.assertEqual(settings["notes"], [])
        _, segs = self.board("""
            segments:
              - {type: clip, src: phone.mp4, in: 0, out: 20, duration: 6, inset: {src: phone.mp4, in: 0, out: 20, speed: 1, at: 3}}
        """)
        self.assertEqual(segs[0].inset_at, 3.0)
        for bad, word in (("{type: title, duration: 3, inset: {src: phone.mp4}}", "only goes on a clip"),
                          ("{type: clip, src: phone.mp4, inset: {src: phone.mp4, at: 99}}", "outside the segment"),
                          ("{type: clip, src: phone.mp4, inset: nope}", "must be a mapping")):
            with self.assertRaises(StoryboardError) as cm:
                self.board(f"segments:\n  - {bad}")
            self.assertIn(word, str(cm.exception))

    def test_a_storyboard_over_three_minutes_is_refused_before_rendering(self):
        path = self.dir / "long.yaml"
        path.write_text("segments:\n" + "".join("  - {type: title, duration: 50, title: x}\n" for _ in range(4)))
        with self.assertRaises(StoryboardError) as cm:
            edit.render(path, root=self.dir)
        self.assertIn("over the limit", str(cm.exception))


class VoiceTest(unittest.TestCase):
    def test_the_voice_recording_is_lined_up_with_the_picture(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        take = write_take(d / "rec", "common1")
        (take / "audio.wav").write_bytes(b"")
        meta = json.loads((take / "meta.json").read_text())
        meta["sources"]["audio"] = {"started": START - 2.0, "file": "audio.wav"}         # parecord started 2 s before the face
        (take / "meta.json").write_text(json.dumps(meta))
        (d / "b.yaml").write_text(f"recordings: {d / 'rec'}\nsegments:\n"
                                  "  - {type: clip, take: common1, source: face, in: 1.5, out: 6.5, voice: {take: common1, source: audio}}\n")
        _, segs = load_storyboard(d / "b.yaml", probe_fn=fake_probe(30), root=d)
        v = segs[0].voice
        self.assertEqual((v.file.name, v.start, v.end), ("audio.wav", 3.5, 8.5))       # face 1.5 s = audio 3.5 s


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
            self.assertAlmostEqual(info["duration"], 25.0, delta=0.3)
            self.assertTrue(info["audio"])
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0",
                                      str(out)], capture_output=True, text=True).stdout
            self.assertIn("960,540,30/1", streams)
            self.assertTrue((out.parent / "stills" / "still_001.00s.png").exists())
            # the clip starts at 4.0 s; its pop-up is there from 0.5 s to 1.5 s into it, in the top right corner, and gone before and after
            from PIL import Image
            import numpy as np
            edit.export_stills(out, [4.2, 4.9, 5.8], out.parent / "inset")
            corner = [np.asarray(Image.open(out.parent / "inset" / f"still_{t:06.2f}s.png").convert("L"), dtype=float)[60:260, -300:-20]
                      for t in (4.2, 4.9, 5.8)]
            self.assertGreater(np.abs(corner[1] - corner[0]).mean(), 8)
            self.assertLess(np.abs(corner[2] - corner[0]).mean(), 8)


if __name__ == "__main__":
    unittest.main()


class VideoFilesTest(unittest.TestCase):
    """The real storyboard and the voiceover script stay in step, and the cut fits in three minutes."""

    def test_the_storyboard_voiceover_is_the_script_and_under_250_words(self):
        import yaml
        root = Path(__file__).resolve().parent.parent
        board = yaml.safe_load((root / "video" / "storyboard.yaml").read_text())
        vo = " ".join(s["vo"] for s in board["segments"] if s.get("vo"))
        script = (root / "video" / "voiceover.md").read_text().split("## The whole script", 1)[1]
        self.assertEqual(" ".join(vo.split()), " ".join(script.split()))
        self.assertLess(len(script.split()), 250)

    def test_the_planned_cut_is_under_three_minutes(self):
        root = Path(__file__).resolve().parent.parent
        try:
            probe_fn = edit.probe if shutil.which("ffprobe") else fake_probe(30.0)       # the real footage's lengths where it exists
            settings, segs = load_storyboard(root / "video" / "storyboard.yaml", probe_fn=probe_fn, root=root)
        except StoryboardError as exc:
            self.skipTest(f"footage on this machine does not match: {exc}")
        self.assertLessEqual(edit.total_duration(segs), settings["max_seconds"])
