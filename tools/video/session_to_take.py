"""A take from a real run: the robot program's session log + a screen recording of that run, ready for the editor.

    python tools/video/session_to_take.py --log data/logs/session-20261119-171941.jsonl \\
        --video "Screencast from 2026-11-19 17-19-57.mp4" --start 17:19:57 --take rfid1 --voice

The robot writes data/logs/session-*.jsonl while it runs (every event, with the time). A screen recording has no events of its own, so
this makes recordings/<take>/ from the two: events.jsonl (person_detected, speech_in, reply, speak, rfid_read, screen_text, zone, as
`sim --film` writes them), meta.json with the clip's start time (--start is the local time the recording began: the file name of a
GNOME screencast, "17-19-57"), and screen.mp4. The editor then captions Stuart's lines at the moments he said them, and can crop the
recording to the part worth showing (crop: in the storyboard).

A run recorded WITH sound: start `python tools/video/film.py record --take rfid3 --sources audio,mic` just before the run (Stuart's
speaker and the microphone, with the same clock as the log), press Enter when it is over (it says whether each heard anything), then
add `--sound-take rfid3` here. That is the real voice of both; `--sound-offset` nudges it if the lip movement is out of step.

A recording that has sound of its own keeps it: nothing is added then (no TTS, no stand-in), and that sound is the take's audio.wav.

--voice: a screen recording usually has no sound, so Stuart is silent in it. With --voice each line he said is made again with the TTS
and placed where the log says he started it (the state that first showed the words), sped up a little if it is longer than the robot's own
voice was (the log notes a reply when it has been spoken), giving recordings/<take>/stuart.wav. What the visitor SAID (speech_in from the microphone; a typed line makes no sound) is added
where the log says they spoke, to give recordings/<take>/audio.wav, to use as `voice: {take: ..., source: audio}`: the visitor's own recording
(recordings/<take>/visitor/<n>.wav, made with `python tools/video/voiceover.py visitor --take <take>`) where there is one, else a
pitched-up stand-in voice. `--remix` makes audio.wav again after a recording changes. Nothing of the real voice was recorded: the robot
does not keep the microphone's sound. The words and the timing are the robot's; the voice is the TTS (the AI server's, or the
local Piper voice), not a recording of the run: say so if it matters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.video import edit  # noqa: E402

WORDS_PER_SECOND = 2.6          # a spoken line's length when it is not synthesised
VISITOR_LAG = 1.0               # seconds between the visitor's last word and the log's UserUtterance (the recording stops, then it is transcribed)
STAND_IN_PITCH = 1.22           # the stand-in for the visitor is the TTS voice raised by this factor, so it is not mistaken for Stuart


def read_log(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def start_epoch(rows: list, hms: str) -> float:
    day = dt.datetime.fromtimestamp(rows[0]["t"]).date()
    h, m, s = hms.replace("-", ":").split(":")
    whole = dt.datetime.combine(day, dt.time(int(h), int(m), int(float(s)))).timestamp()
    return whole + (float(s) - int(float(s)))                      # seconds may have a fraction: 17:19:57.55


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())


def spoken_start(rows: list, index: int) -> float:
    """When Stuart began saying the reply logged at rows[index] (the log notes a reply when it has been spoken): the time of the state that
    first showed the same words, looking back from the reply, or the reply's own time if there is none."""
    text = rows[index]["data"]["text"]
    start = rows[index]["t"]
    for r in reversed(rows[:index]):
        if r["event"] == "ReplySentence":
            break                                              # the line before it: everything after that is this one
        if r["event"] == "StateEntered":
            shown = (r["data"].get("text") or "").strip()
            if shown and (text.startswith(shown[:40]) or shown.startswith(text[:40])):
                start = r["t"]
    return start


def fit_to_slot(wav: Path, slot: float, longest_speedup: float = 1.5) -> Path:
    """The line sped up (pitch kept) if the TTS takes longer than the robot's own voice did, so it never runs into the next line."""
    length = wav_seconds(wav)
    if length <= slot + 0.05:
        return wav
    import subprocess
    out = wav.with_name("fit.wav")
    rate = min(longest_speedup, length / slot)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-filter:a", f"atempo={rate:.4f}", str(out)], check=True)
    return out


def has_audio(path: Path) -> bool:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    return "audio" in out.stdout


def use_recorded_sound(video: Path, take: Path, t0: float) -> Path:
    """The recording has its own sound: that is the voice (Stuart's and the visitor's), so nothing is synthesised. It becomes audio.wav."""
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "48000", str(take / "audio.wav")], check=True)
    meta = json.loads((take / "meta.json").read_text())
    meta["sources"]["audio"] = {"file": "audio.wav", "started": t0, "error": None, "note": "the sound of the screen recording itself"}
    (take / "meta.json").write_text(json.dumps(meta, indent=2))
    return take / "audio.wav"


def mix_recorded_sound(sound: Path, take: Path, t0: float, duration: float, offset: float = 0.0) -> Path:
    """The sound that film.py recorded beside the screen recording (recordings/<sound>/audio.wav = Stuart's speaker, mic.wav = the visitor's
    microphone), lined up with the picture by the start times in its meta.json (`offset` seconds later if the picture runs ahead of it), as
    the take's audio.wav. Nothing is synthesised."""
    meta = json.loads((sound / "meta.json").read_text())
    inputs, graph, labels = [], [], []
    for name in ("audio", "mic"):
        info = meta.get("sources", {}).get(name)
        if not info or info.get("error") or not (sound / info["file"]).is_file():
            continue
        shift = info["started"] - t0 + offset                   # seconds after the picture's first frame that this sound starts
        i = len(labels)
        inputs += ["-i", str(sound / info["file"])]
        trim = f"atrim=start={-shift:.3f},asetpts=PTS-STARTPTS," if shift < 0 else ""
        delay = f",adelay={int(shift * 1000)}|{int(shift * 1000)}" if shift > 0 else ""
        graph.append(f"[{i}:a]{trim}aresample=48000,aformat=channel_layouts=mono{delay}[s{i}]")
        labels.append(f"[s{i}]")
    if not labels:
        raise SystemExit(f"recordings/{sound.name} has no audio.wav or mic.wav that recorded (see its meta.json)")
    graph.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0,apad=whole_dur={duration:.3f}[out]")
    edit.run_ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-t", f"{duration:.3f}", str(take / "audio.wav")], "the recorded sound")
    meta_take = json.loads((take / "meta.json").read_text())
    meta_take["sources"]["audio"] = {"file": "audio.wav", "started": t0, "error": None,
                                     "note": f"the real sound, recorded by film.py in recordings/{sound.name} ({', '.join(n for n in ('audio', 'mic') if n in meta.get('sources', {}))})"}
    (take / "meta.json").write_text(json.dumps(meta_take, indent=2))
    return take / "audio.wav"


def visitor_lines(events: list) -> list:
    """[(number, end, text)] of what the visitor SAID (the microphone: a typed line makes no sound), end = when the last word was spoken, on
    the same clock as the events."""
    spoken = [e for e in events if e["type"] == "speech_in" and e.get("source") == "mic"]
    return [(n, e["t"] - VISITOR_LAG, e["text"]) for n, e in enumerate(spoken, 1)]


def wav_rate(path: Path) -> int:
    with wave.open(str(path)) as w:
        return w.getframerate()


def stand_in(text: str, path: Path) -> bool:
    """The visitor's line with the TTS, pitched up: only so the conversation can be heard until the visitor records their own words."""
    import subprocess
    raw = path.with_name(path.stem + "_raw.wav")
    if not edit.tts_line(text, raw):
        return False
    rate = wav_rate(raw)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-filter:a",
                    f"asetrate={int(rate * STAND_IN_PITCH)},aresample={rate},atempo={1 / STAND_IN_PITCH:.4f}", str(path)], check=True)
    raw.unlink()
    return True


def visitor_pieces(take: Path, events: list, t0: float) -> list:
    """[(start seconds into the take's recording, wav)] for what the visitor said: the visitor's own recording
    (recordings/<take>/visitor/<n>.wav, from `voiceover.py visitor`) if there is one, else the stand-in voice (made once, kept next to it)."""
    folder = take / "visitor"
    out = []
    for n, end, text in visitor_lines(events):
        wav = edit.find_recording(folder, str(n))
        if wav is None:
            wav = folder / f"standin_{n}.wav"
            if not wav.is_file():
                folder.mkdir(parents=True, exist_ok=True)
                if not stand_in(text, wav):
                    raise SystemExit("the TTS is not reachable, so the visitor's stand-in voice cannot be made (start the local AI: docs/local_ai.md)")
        length = edit.probe(wav)["duration"]
        out.append((max(0.0, end - t0 - length), wav))
    return out


def mix_take_audio(take: Path) -> Path:
    """recordings/<take>/audio.wav = Stuart's lines (stuart.wav) + what the visitor said (their own recordings, else the stand-in voice)."""
    meta = json.loads((take / "meta.json").read_text())
    t0 = meta["start_time"]
    events = [json.loads(l) for l in (take / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    pieces = []
    if (take / "stuart.wav").is_file():
        pieces.append((0.0, take / "stuart.wav"))
    pieces += visitor_pieces(take, events, t0)
    if not pieces:
        raise SystemExit("nothing to mix: no stuart.wav and no spoken visitor lines")
    duration = edit.probe(take / meta["sources"]["screen"]["file"])["duration"]
    edit.mix_at(pieces, take / "audio.wav", duration)
    meta["sources"]["audio"] = {"file": "audio.wav", "started": t0, "error": None,
                                "note": "Stuart's lines made again with the TTS where the log says he spoke, and the visitor's words: their own recording "
                                        "where there is one (visitor/<n>.wav), else a pitched-up stand-in (visitor/standin_<n>.wav)"}
    (take / "meta.json").write_text(json.dumps(meta, indent=2))
    return take / "audio.wav"


def events_from_log(rows: list, t0: float, t1: float) -> tuple:
    """(events, replies): the events of the log in [t0, t1] as film events, and the spoken replies as (start, end, text) for the voice."""
    events, replies, last_screen = [], [], ""
    for n, r in enumerate(rows):
        if not t0 - 2 <= r["t"] <= t1 + 2:
            continue
        d, kind, t = r["data"], r["event"], round(r["t"], 3)
        if kind == "PersonEngaged":
            events.append({"t": t, "type": "person_detected", "source": d.get("source")})
        elif kind == "UserUtterance":
            events.append({"t": t, "type": "speech_in", "text": d["text"], "source": d.get("source")})
        elif kind == "BookPlaced":
            events.append({"t": t, "type": "book_placed", "source": d.get("source")})
        elif kind == "BookIdentified":
            events.append({"t": t, "type": "rfid_read", "title": d.get("title"), "isbn": d.get("isbn")})
        elif kind == "ReplySentence":
            channel = "speech" if "speech" in ((r.get("style") or {}).get("channels") or []) else "screen"
            events.append({"t": t, "type": "reply", "text": d["text"], "channel": channel, "zone": (r.get("context") or {}).get("zone")})
            if channel == "speech":
                replies.append((spoken_start(rows, n), r["t"], d["text"]))
        elif kind == "ScreenUpdate" and d.get("zone") == "quiet":
            payload = d.get("payload") or {}
            text = payload.get("message") or payload.get("caption") or d.get("text") or ""
            if text and text != last_screen:
                last_screen = text
                events.append({"t": t, "type": "screen_text", "text": text, "view": d.get("view")})
        elif kind == "StateEntered":
            events.append({"t": t, "type": "state", "state": d.get("state_path"), "zone": d.get("zone")})
    zone = next((r["context"]["zone"] for r in rows if r.get("context") and r["t"] >= t0), None)
    if zone:
        events.insert(0, {"t": round(t0, 3), "type": "zone", "zone": zone, "source": "log"})
    events.insert(0, {"t": round(t0, 3), "type": "start", "take": "from a session log", "base": "real"})
    return events, replies


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="session_to_take.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log")
    p.add_argument("--video")
    p.add_argument("--start", help="local time the first frame was recorded, HH:MM:SS[.fraction] (a GNOME screencast's name is "
                                                  "about 0.5 s early: 17-19-57; check it against a state change seen in both)")
    p.add_argument("--take", required=True)
    p.add_argument("--remix", action="store_true", help="only make recordings/<take>/audio.wav again (Stuart's lines + the visitor's recordings)")
    p.add_argument("--voice", action="store_true", help="make Stuart's lines with the TTS and place them (the recording has no sound)")
    p.add_argument("--sound-take", help="recordings/<name>: sound recorded by `film.py record --sources audio,mic` during the run; it is "
                                        "put under the picture as it is (no TTS, no stand-in)")
    p.add_argument("--sound-offset", type=float, default=0.0, help="seconds to move that sound later (negative: earlier) if it is out of step")
    p.add_argument("--source", default="screen", help="the name of the recording in the take (default %(default)s)")
    args = p.parse_args(argv)
    if args.remix:
        print(f"wrote {mix_take_audio(ROOT / 'recordings' / args.take)}")
        return 0
    if not (args.log and args.video and args.start):
        p.error("--log, --video and --start are needed (or --remix)")
    rows = read_log(Path(args.log))
    video = Path(args.video)
    t0 = start_epoch(rows, args.start)
    info = edit.probe(video)
    take = ROOT / "recordings" / args.take
    take.mkdir(parents=True, exist_ok=True)
    shutil.copy(video, take / f"{args.source}.mp4")
    events, replies = events_from_log(rows, t0, t0 + info["duration"])
    meta = {"take": args.take, "start_time": t0, "clock": "time.time()", "from_log": str(args.log),
            "sources": {args.source: {"file": f"{args.source}.mp4", "started": t0, "error": None}}}
    pieces, speak = [], []
    own_sound = has_audio(video)
    if args.sound_take:
        args.voice = False
    if own_sound and args.voice:
        print("the recording has its own sound: it is used as it is and nothing is synthesised (no TTS, no stand-in)")
        args.voice = False
    for begin, end, text in replies:
        wav = Path(tempfile.mkdtemp()) / "line.wav"
        slot = max(0.5, end - begin)                                     # how long the robot's own voice took
        if args.voice:
            if not edit.tts_line(text, wav):
                raise SystemExit("--voice: the TTS is not reachable (start the local AI: docs/local_ai.md); nothing was written")
            wav = fit_to_slot(wav, slot)
            length = min(wav_seconds(wav), slot) if slot else wav_seconds(wav)
            pieces.append((max(0.0, begin - t0), wav))
        else:
            length = len(text.split()) / WORDS_PER_SECOND + 0.3
        speak.append({"t": round(begin, 3), "type": "speak", "text": text, "seconds": round(length, 2)})
    if args.voice and pieces:
        edit.mix_at(pieces, take / "stuart.wav", info["duration"])
    events += speak
    events.sort(key=lambda e: e["t"])
    (take / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    (take / "meta.json").write_text(json.dumps(meta, indent=2))
    if args.voice:
        mix_take_audio(take)
        meta = json.loads((take / "meta.json").read_text())
    elif args.sound_take:
        mix_recorded_sound(ROOT / "recordings" / args.sound_take, take, t0, info["duration"], args.sound_offset)
        meta = json.loads((take / "meta.json").read_text())
    elif own_sound:
        use_recorded_sound(video, take, t0)
        meta = json.loads((take / "meta.json").read_text())
    print(f"recordings/{args.take}/: {args.source}.mp4 ({info['duration']:.1f} s), {len(events)} events, {len(speak)} spoken line(s)"
          + (", audio.wav (TTS voice)" if "audio" in meta["sources"] else ""))
    for e in events:
        if e["type"] in ("person_detected", "speech_in", "speak", "rfid_read"):
            print(f"  {e['t'] - t0:6.1f}  {e['type']:16} {(e.get('text') or e.get('title') or '')[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
