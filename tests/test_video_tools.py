"""The tools around the editor: replaying a real drive, the trajectory from a log, voiceover cues and recordings, a take from a session log."""

import datetime as dt
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.fake_base import FakeBase, replay_pose
from tools.video import edit, session_to_take, trajectory, voiceover
from tests.test_video_edit import START, fake_probe, write_take


class ReplayPoseTest(unittest.TestCase):
    TRACK = [[0, 0.0, 0.0, 0.0], [10, 1.0, 2.0, 1.0], [20, 1.0, 2.0, 1.0]]

    def test_straight_lines_between_the_samples_and_staying_put_at_the_ends(self):
        self.assertEqual(replay_pose(self.TRACK, -5), (0.0, 0.0, 0.0))
        x, y, a = replay_pose(self.TRACK, 5)
        self.assertEqual((round(x, 3), round(y, 3), round(a, 3)), (0.5, 1.0, 0.5))
        self.assertEqual(replay_pose(self.TRACK, 99), (1.0, 2.0, 1.0))

    def test_the_heading_turns_the_short_way_round(self):
        a = replay_pose([[0, 0, 0, 3.0], [10, 0, 0, -3.0]], 5)[2]
        self.assertGreater(abs(a), 3.0)                         # through pi, not back through 0


class FakeBaseReplayTest(unittest.TestCase):
    def base(self, track):
        now = [100.0]
        b = FakeBase("t", clock=lambda: now[0], wall=lambda: now[0], replay=[track])
        b.last_heartbeat = now[0]
        return b, now

    def test_a_goal_follows_the_recorded_track_at_its_own_speed_then_succeeds(self):
        b, now = self.base([[0, 0.0, 0.0, 0.0], [10, 0.0, 4.0, 1.5], [12, 0.1, 4.0, 1.5]])
        status, body = b.send_goal("g-1", 0.0, 4.0, 1.5)
        self.assertTrue(body["accepted"])
        now[0] += 5.0
        b.last_heartbeat = now[0]
        b.update()
        self.assertEqual((round(b.x, 2), round(b.y, 2)), (0.0, 2.0))
        self.assertEqual(b.goal["status"], "active")
        now[0] += 8.0
        b.last_heartbeat = now[0]
        b.update()
        self.assertEqual((round(b.x, 2), round(b.y, 2), b.goal["status"]), (0.1, 4.0, "succeeded"))

    def test_without_a_track_it_still_drives_in_a_straight_line(self):
        now = [100.0]
        b = FakeBase("t", clock=lambda: now[0], wall=lambda: now[0], speed=1.0)
        b.last_heartbeat = now[0]
        b.send_goal("g-1", 3.0, 0.0, 0.0)
        now[0] += 1.0
        b.last_heartbeat = now[0]
        b.update()
        self.assertAlmostEqual(b.x, 1.0, places=2)


class TrajectoryTest(unittest.TestCase):
    def log(self, d):
        base = dt.datetime(2026, 11, 18, 18, 27, 0).timestamp()
        rows = [{"t": base + 30, "event": "PoseUpdated", "data": {"x": 0.0, "y": 0.0, "yaw": 0.0, "localised": True}},
                {"t": base + 35, "event": "NavProgress", "data": {"goal_id": "g-1", "distance_left": 4.0}},
                {"t": base + 40, "event": "PoseUpdated", "data": {"x": 0.0, "y": 1.0, "yaw": 1.5, "localised": True}},
                {"t": base + 45, "event": "PoseUpdated", "data": {"x": 0.0, "y": 2.0, "yaw": 1.5, "localised": True}},
                {"t": base + 50, "event": "NavArrived", "data": {"goal_id": "g-1"}},
                {"t": base + 60, "event": "PoseUpdated", "data": {"x": 0.0, "y": 2.2, "yaw": 1.5, "localised": True}}]
        path = Path(d) / "log.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path, base

    def test_a_window_becomes_a_track_from_where_the_robot_stood(self):
        with tempfile.TemporaryDirectory() as d:
            path, base = self.log(d)
            rows = trajectory.read_log(path)
            self.assertEqual(trajectory.drives(rows)[0][0], "g-1")
            t0, t1 = trajectory.parse_window("18:27:35-18:27:55", trajectory.local_day(rows))
            self.assertEqual((t0, t1), (base + 35, base + 55))
            tr = trajectory.track(rows, t0, t1, "out")
            self.assertEqual(tr["points"][0], [0.0, 0.0, 0.0, 0.0])             # the pose before the window
            self.assertEqual([p[0] for p in tr["points"]], [0.0, 5.0, 10.0, 20.0])   # then the poses inside, then it waits until the end
            self.assertEqual(tr["points"][-1][1:], tr["points"][-2][1:])
            with self.assertRaises(ValueError):
                trajectory.track([], 1.0, 2.0)

    def test_the_cli_writes_the_file_the_sim_reads(self):
        from robot.sim.base import load_replay
        with tempfile.TemporaryDirectory() as d:
            path, _ = self.log(d)
            out = Path(d) / "t.json"
            self.assertEqual(trajectory.main([str(path), "--window", "18:27:35-18:27:55", "-o", str(out)]), 0)
            self.assertEqual(len(load_replay(out)), 1)


class CropAndVoiceoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        write_take(self.dir / "rec", "t1")
        (self.dir / "a.png").write_bytes(b"")

    def board(self, body):
        path = self.dir / "b.yaml"
        path.write_text(f"recordings: {self.dir / 'rec'}\n" + body)
        return edit.load_storyboard(path, probe_fn=fake_probe(30.0), root=self.dir)

    def test_a_crop_is_kept_on_the_pane_and_checked(self):
        _, segs = self.board("segments:\n  - {type: clip, take: t1, source: face, in: 1, out: 5, crop: [10, 20, 300, 200]}\n")
        self.assertEqual(segs[0].panes[0].crop, (10, 20, 300, 200))
        self.assertIn("crop=300:200:10:20", edit.pane_video_filter(0, segs[0].panes[0], 960, 540, 4.0))
        with self.assertRaises(edit.StoryboardError):
            self.board("segments:\n  - {type: clip, take: t1, source: face, crop: [1, 2, 3]}\n")

    def test_a_line_has_the_time_until_the_next_line_not_just_its_segment(self):
        _, segs = self.board("""segments:
  - {id: a, type: title, duration: 4, title: x, vo: first}
  - {id: b, type: title, duration: 3, title: y}
  - {id: c, type: title, duration: 5, title: z, vo: second}
  - {id: d, type: title, duration: 2, title: w}
""")
        self.assertEqual(edit.vo_slots(segs), {0: 7.0, 2: 7.0})

    def test_a_recording_is_found_by_the_segment_id_with_any_audio_extension(self):
        d = self.dir / "voice"
        d.mkdir()
        self.assertIsNone(edit.find_recording(d, "robot-concept"))
        (d / "robot-concept.m4a").write_bytes(b"x")
        self.assertEqual(edit.find_recording(d, "robot-concept").name, "robot-concept.m4a")

    def test_the_cue_sheet_names_each_file_and_marks_the_recorded_ones(self):
        _, segs = self.board("""segments:
  - {id: first, type: title, duration: 4, title: x, vo: "one two three"}
  - {id: second, type: title, duration: 5, title: y, vo: "four five"}
""")
        d = self.dir / "voice"
        d.mkdir()
        (d / "second.wav").write_bytes(b"x")
        rows = voiceover.cue_rows(segs, d)
        self.assertEqual([(r["id"], r["recorded"], r["slot"]) for r in rows], [("first", False, 4.0), ("second", True, 5.0)])
        sheet = voiceover.cue_sheet(rows)
        self.assertIn("`first.wav`", sheet)
        self.assertIn("`second.wav` ✓", sheet)
        self.assertIn("5 words", voiceover.script_md(rows))


class SessionToTakeTest(unittest.TestCase):
    def test_a_session_log_becomes_events_with_the_voice_lines_placed_where_the_log_says(self):
        t0 = dt.datetime(2026, 11, 19, 17, 19, 57).timestamp()
        ctx = {"zone": "common"}
        style = {"channels": ["speech", "screen"]}
        rows = [{"t": t0 + 1, "event": "ContextChanged", "data": {}, "context": ctx, "style": style},
                {"t": t0 + 8.5, "event": "PersonEngaged", "data": {"source": "sensor"}, "context": ctx, "style": style},
                {"t": t0 + 8.5, "event": "StateEntered", "data": {"state_path": "ENGAGED.GREET", "text": "Hello! My name is Stuart."}, "context": ctx, "style": style},
                {"t": t0 + 11.6, "event": "ReplySentence", "data": {"text": "Hello! My name is Stuart."}, "context": ctx, "style": style},
                {"t": t0 + 30.5, "event": "UserUtterance", "data": {"text": "i would like to return the book", "source": "operator"}, "context": ctx, "style": style},
                {"t": t0 + 42.3, "event": "BookIdentified", "data": {"title": "Dune", "isbn": "9780441172719"}, "context": ctx, "style": style}]
        events, replies = session_to_take.events_from_log(rows, t0, t0 + 70)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[:2], ["start", "zone"])
        for kind in ("person_detected", "reply", "speech_in", "rfid_read"):
            self.assertIn(kind, kinds)
        self.assertEqual(replies, [(t0 + 8.5, t0 + 11.6, "Hello! My name is Stuart.")])      # started with the state that showed it
        self.assertEqual(next(e for e in events if e["type"] == "reply")["channel"], "speech")
        self.assertAlmostEqual(session_to_take.start_epoch(rows, "17:19:57.55"), t0 + 0.55, places=2)

    def test_a_line_starts_with_the_state_that_first_showed_it_not_with_an_earlier_line(self):
        rows = [{"t": 1.0, "event": "StateEntered", "data": {"text": "Hello! My name is Stuart."}},
                {"t": 4.0, "event": "ReplySentence", "data": {"text": "Hello! My name is Stuart."}},
                {"t": 4.0, "event": "StateEntered", "data": {"text": "I can take back a book, tell you about one. What would you like?"}},
                {"t": 4.0, "event": "StateEntered", "data": {"text": "Hello! My name is Stuart."}},          # the old text again: not this line's
                {"t": 11.0, "event": "ReplySentence", "data": {"text": "I can take back a book, tell you about one. What would you like?"}}]
        self.assertEqual(session_to_take.spoken_start(rows, 1), 1.0)
        self.assertEqual(session_to_take.spoken_start(rows, 4), 4.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_line_longer_than_the_robots_voice_is_sped_up_to_fit(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        wav = d / "line.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(wav)], check=True)
        fitted = session_to_take.fit_to_slot(wav, 4.0)
        self.assertAlmostEqual(session_to_take.wav_seconds(fitted), 4.0, delta=0.15)
        self.assertEqual(session_to_take.fit_to_slot(wav, 7.0), wav)                       # shorter: left alone
        self.assertGreater(session_to_take.wav_seconds(session_to_take.fit_to_slot(wav, 2.0)), 3.9)    # never faster than x1.5

    def test_only_what_the_visitor_said_aloud_gets_a_voice(self):
        events = [{"t": 30.0, "type": "speech_in", "text": "Can I return a book?", "source": "mic"},
                  {"t": 40.0, "type": "speech_in", "text": "typed by the operator", "source": "operator"},
                  {"t": 50.0, "type": "speech_in", "text": "Thank you", "source": "mic"}]
        lines = session_to_take.visitor_lines(events)
        self.assertEqual([(n, text) for n, _, text in lines], [(1, "Can I return a book?"), (2, "Thank you")])
        self.assertAlmostEqual(lines[0][1], 30.0 - session_to_take.VISITOR_LAG)        # their last word came just before the log's time

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_recording_with_its_own_sound_keeps_it_and_nothing_is_added(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        loud, silent = d / "loud.mp4", d / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
                        "-shortest", str(loud)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=2", str(silent)], check=True)
        self.assertTrue(session_to_take.has_audio(loud))
        self.assertFalse(session_to_take.has_audio(silent))
        take = d / "take"
        take.mkdir()
        (take / "meta.json").write_text('{"sources": {}}')
        wav = session_to_take.use_recorded_sound(loud, take, 0.0)
        self.assertGreater(session_to_take.wav_seconds(wav), 1.5)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_sound_recorded_beside_the_screen_recording_is_lined_up_by_the_start_times(self):
        import json
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        sound, take = d / "sound", d / "take"
        sound.mkdir(), take.mkdir()
        # a 1 s tone that starts 2 s after the picture, and a microphone file that failed
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", str(sound / "audio.wav")], check=True)
        (sound / "meta.json").write_text(json.dumps({"sources": {"audio": {"file": "audio.wav", "started": 102.0, "error": None},
                                                                  "mic": {"file": "mic.wav", "started": 100.0, "error": "no microphone"}}}))
        (take / "meta.json").write_text(json.dumps({"sources": {}}))
        out = session_to_take.mix_recorded_sound(sound, take, 100.0, 5.0)
        self.assertAlmostEqual(session_to_take.wav_seconds(out), 5.0, delta=0.1)
        levels = []
        for a, b in ((0.0, 1.8), (2.2, 2.8), (3.3, 4.8)):
            res = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            levels.append(float(res.split("max_volume: ")[1].split(" dB")[0]))
        self.assertLess(levels[0], -60)          # nothing before it starts
        self.assertGreater(levels[1], -30)       # heard at 2 s
        self.assertLess(levels[2], -60)          # and over by 3 s
        self.assertEqual(json.loads((take / "meta.json").read_text())["sources"]["audio"]["file"], "audio.wav")


if __name__ == "__main__":
    unittest.main()


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@unittest.skipUnless(have_ffmpeg(), "needs ffmpeg")
class BuildVoiceoverTest(unittest.TestCase):
    def tone(self, path: Path, seconds: float) -> Path:
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path)], check=True)
        return path

    def test_a_recorded_line_replaces_the_tts_and_a_long_one_warns(self):
        import hashlib
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "t.png").write_bytes(b"")
        board = d / "b.yaml"
        board.write_text("segments:\n  - {id: one, type: title, duration: 4, title: x, vo: hello there}\n"
                         "  - {id: two, type: title, duration: 4, title: y, vo: second line}\n")
        _, segs = edit.load_storyboard(board, probe_fn=fake_probe(), root=d)
        tts = d / "tts"
        tts.mkdir()
        for seg in segs:                                           # the TTS is already made (cached): no server needed
            self.tone(tts / f"{seg.spec['id']}.{hashlib.sha1(seg.vo.encode()).hexdigest()[:8]}.wav", 1.0)
        rec = d / "rec"
        rec.mkdir()
        self.tone(rec / "one.wav", 6.5)                            # your recording: longer than its 4 s
        out, how, warnings = edit.build_voiceover(segs, d / "mix.wav", 8.0, d, {"dir": "rec", "tts_dir": "tts"})
        self.assertEqual(how, "1 line(s) recorded by you, 1 from the TTS")
        self.assertTrue(any("one:" in w and "6.5 s" in w for w in warnings), warnings)
        self.assertAlmostEqual(edit.probe(out)["duration"], 8.0, delta=0.2)
        # the second line waited for the first (6.5 s long from 0.3 s) rather than speaking over it
        self.assertTrue(any("two:" in w and "late" in w for w in warnings), warnings)


class ReverseClipTest(unittest.TestCase):
    def test_piece_bounds_cover_the_clip(self):
        from tools.video.reverse import piece_bounds
        self.assertEqual(piece_bounds(10.0, 4.0), [(0.0, 4.0), (4.0, 4.0), (8.0, 2.0)])
        self.assertEqual(piece_bounds(4.0, 4.0), [(0.0, 4.0)])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_the_clip_plays_backwards_in_pieces(self):
        from tools.video.reverse import reverse_clip, duration
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "a.mp4", Path(d) / "b.mp4"
            # red for the first 2 s, then blue for 2 s: reversed it is blue first
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=64x48:r=30:d=2", "-f", "lavfi", "-i",
                            "color=blue:s=64x48:r=30:d=2", "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", str(src)], check=True)
            reverse_clip(src, dst, piece=1.5)
            self.assertAlmostEqual(duration(dst), 4.0, delta=0.15)
            def pixel(t):
                raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(dst), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                     capture_output=True).stdout
                return raw[0], raw[2]                              # red, blue of the first pixel
            r0, b0 = pixel(0.3)
            r1, b1 = pixel(3.7)
            self.assertGreater(b0, 150)
            self.assertLess(r0, 100)
            self.assertGreater(r1, 150)


class FilmMicTest(unittest.TestCase):
    def test_the_microphone_is_a_source_that_records_what_the_visitor_says(self):
        from unittest import mock
        from tools.video import film
        self.assertIn("mic", film.SOURCES)
        with mock.patch.object(film, "mic_input", return_value="alsa_input.usb-MV_USB_MIC") as pick:
            sources = film.plan_sources(["mic", "audio"], Path("/tmp/take"), "http://x", "USB", "MV_USB")
        pick.assert_called_once_with("MV_USB")
        mic = sources[0]
        self.assertEqual(mic.output.name, "mic.wav")
        self.assertIn("--device=alsa_input.usb-MV_USB_MIC", mic.cmds[0])
        self.assertIn("--channels=1", mic.cmds[0])
        with mock.patch.object(film, "mic_input", return_value=None):
            self.assertEqual(film.plan_sources(["mic"], Path("/tmp/take"), "http://x", None)[0].error, "no microphone to record from")

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_silent_recording_is_told_from_one_that_heard_something(self):
        from tools.video import film
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        for name, src in (("tone", "sine=f=440:d=1"), ("silence", "anullsrc=r=48000:cl=mono")):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", "1", str(d / f"{name}.wav")], check=True)
        self.assertGreater(film.sound_level(d / "tone.wav")[1], -30)
        self.assertLess(film.sound_level(d / "silence.wav")[1], -50)
