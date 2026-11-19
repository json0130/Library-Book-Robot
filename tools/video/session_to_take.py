"""A take from a real run: the robot program's session log + a screen recording of that run, ready for the editor.

    python tools/video/session_to_take.py --log data/logs/session-20261119-171941.jsonl \\
        --video "Screencast from 2026-11-19 17-19-57.mp4" --start 17:19:57 --take rfid1 --voice

The robot writes data/logs/session-*.jsonl while it runs (every event, with the time). A screen recording has no events of its own, so
this makes recordings/<take>/ from the two: events.jsonl (person_detected, speech_in, reply, speak, rfid_read, screen_text, zone, as
`sim --film` writes them), meta.json with the clip's start time (--start is the local time the recording began: the file name of a
GNOME screencast, "17-19-57"), and screen.mp4. The editor then captions Stuart's lines at the moments he said them, and can crop the
recording to the part worth showing (crop: in the storyboard).

--voice: a screen recording usually has no sound, so Stuart is silent in it. With --voice each line he said is made again with the TTS
and placed so that it ENDS when the log says he finished it (the log notes a reply when it has been spoken), giving recordings/<take>/audio.wav
to use as `voice: {take: ..., source: audio}`. The words and the timing are the robot's; the voice is the TTS (the AI server's, or the
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


def events_from_log(rows: list, t0: float, t1: float) -> tuple:
    """(events, replies): the events of the log in [t0, t1] as film events, and the replies as (end time, text) for the voice."""
    events, replies, last_screen = [], [], ""
    for r in rows:
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
                replies.append((r["t"], d["text"]))
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
    p.add_argument("--log", required=True)
    p.add_argument("--video", required=True)
    p.add_argument("--start", required=True, help="local time the first frame was recorded, HH:MM:SS[.fraction] (a GNOME screencast's name is "
                                                  "about 0.5 s early: 17-19-57; check it against a state change seen in both)")
    p.add_argument("--take", required=True)
    p.add_argument("--voice", action="store_true", help="make Stuart's lines with the TTS and place them (the recording has no sound)")
    p.add_argument("--source", default="screen", help="the name of the recording in the take (default %(default)s)")
    args = p.parse_args(argv)
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
    for end, text in replies:
        wav = Path(tempfile.mkdtemp()) / "line.wav"
        if args.voice and edit.tts_line(text, wav):
            length = wav_seconds(wav)
            pieces.append((max(0.0, end - t0 - length), wav))
        else:
            length = len(text.split()) / WORDS_PER_SECOND + 0.3
        speak.append({"t": round(end - length, 3), "type": "speak", "text": text, "seconds": round(length, 2)})
    if args.voice and pieces:
        edit.mix_at(pieces, take / "audio.wav", info["duration"])
        meta["sources"]["audio"] = {"file": "audio.wav", "started": t0, "error": None,
                                    "note": "Stuart's lines made again with the TTS, placed where the log says they ended"}
    events += speak
    events.sort(key=lambda e: e["t"])
    (take / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    (take / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"recordings/{args.take}/: {args.source}.mp4 ({info['duration']:.1f} s), {len(events)} events, {len(speak)} spoken line(s)"
          + (", audio.wav (TTS voice)" if "audio" in meta["sources"] else ""))
    for e in events:
        if e["type"] in ("person_detected", "speech_in", "speak", "rfid_read"):
            print(f"  {e['t'] - t0:6.1f}  {e['type']:16} {(e.get('text') or e.get('title') or '')[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
