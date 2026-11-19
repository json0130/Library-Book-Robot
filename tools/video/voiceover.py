"""The voiceover: the script with its cues, and a recorder that saves each line under the name the editor looks for.

    python tools/video/voiceover.py cues                 # every line: when it starts, how long it has, its file name
    python tools/video/voiceover.py cues --write         # also writes video/voiceover.md (the script to read from)
    python tools/video/voiceover.py record               # read the lines one by one: Enter starts, Enter stops, r retries
    python tools/video/voiceover.py record --only robot-concept --mic USB_MIC
    python tools/video/voiceover.py visitor --take rfid2    # record what the visitor says in the common-area run (your own voice)
    python tools/video/voiceover.py list-mics

A line you record is saved as video/voiceover/<segment id>.wav and replaces that line of the TTS narration in the next render
(`python tools/video/edit.py`). Lines you have not recorded keep the TTS (kept in video/voiceover_tts/ as a backup). To go back to the
TTS for a line, delete or rename its file. If you record the whole thing as one file instead, name it video/voiceover.wav (from 0:00).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.video import edit  # noqa: E402

WORDS_PER_SECOND = 2.3          # a calm pace, about 140 words a minute


def load(storyboard: Path = ROOT / "video" / "storyboard.yaml"):
    settings, segments = edit.load_storyboard(storyboard)
    rec_dir = Path(settings["voiceover"].get("dir", "video/voiceover"))
    rec_dir = rec_dir if rec_dir.is_absolute() else ROOT / rec_dir
    return settings, segments, rec_dir


def cue_rows(segments, rec_dir: Path) -> list:
    rows = []
    slots = edit.vo_slots(segments)
    for i, (s, start) in enumerate(zip(segments, edit.segment_starts(segments))):
        if not s.vo:
            continue
        name = s.spec.get("id") or s.name
        words = len(s.vo.split())
        rows.append({"id": name, "start": start, "slot": slots[i], "words": words, "estimate": words / WORDS_PER_SECOND,
                     "text": s.vo, "recorded": edit.find_recording(rec_dir, name) is not None,
                     "over": s.spec.get("zone") or "", "title": s.spec.get("title") or s.spec.get("text") or ""})
    return rows


def clock(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def cue_sheet(rows: list) -> str:
    out = ["| Starts | File (video/voiceover/) | Time it has | About | Line |", "|---|---|---|---|---|"]
    for r in rows:
        tight = " (tight)" if r["estimate"] > r["slot"] - 0.5 else ""
        out.append(f"| {clock(r['start'])} | `{r['id']}.wav`{' ✓' if r['recorded'] else ''} | {r['slot']:.1f} s | {r['estimate']:.1f} s{tight} | {r['text']} |")
    return "\n".join(out)


def script_md(rows: list) -> str:
    words = sum(r["words"] for r in rows)
    lines = ["# Voiceover script", "",
             f"{len(rows)} lines, {words} words (the limit is 250). Read each at a calm pace, about 140 words a minute, starting at the time on",
             "its left. Stay silent in between, above all in the common-area visit, where Stuart speaks. Save each line as the file named in the table",
             "(`python tools/video/voiceover.py record` does that for you); a line you record replaces the TTS narration for that line, the others keep",
             "the TTS (a backup copy of the narration is in `video/voiceover_tts/`). The times come from `python tools/video/edit.py --plan` and move",
             "if the footage changes: `python tools/video/voiceover.py cues --write` makes this page again.", "",
             cue_sheet(rows), "", "## The whole script", ""]
    for r in rows:
        lines += [r["text"], ""]
    return "\n".join(lines).rstrip() + "\n"


def mic_sources() -> list:
    out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
    return [line.split("\t")[1] for line in out.splitlines() if "\t" in line and not line.split("\t")[1].endswith(".monitor")]


def pick_mic(fragment) -> str:
    sources = mic_sources()
    if fragment:
        hit = [s for s in sources if fragment.lower() in s.lower()]
        if not hit:
            raise SystemExit(f"no microphone matching {fragment!r}; `list-mics` shows them")
        return hit[0]
    return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip() or sources[0]


def peak_db(path: Path):
    """The loudest sample of a recording in dB (-91 is digital silence), or None if it cannot be read."""
    import re
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True).stderr
    hit = re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    return None if not hit else (-91.0 if hit.group(1) == "-inf" else float(hit.group(1)))


def silent(path: Path, floor: float = -50.0) -> bool:
    """True if nothing was recorded (a microphone with nothing plugged in gives a perfectly silent file)."""
    peak = peak_db(path)
    return peak is None or peak < floor


def record_one(path: Path, mic: str, seconds: float = 0.0) -> float:
    """Record 48 kHz mono WAV from the microphone until Enter (or for `seconds`); returns its length."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp()) / "take.wav"
    proc = subprocess.Popen(["parecord", f"--device={mic}", "--rate=48000", "--channels=1", "--format=s16le", "--file-format=wav", str(tmp)])
    started = time.time()
    try:
        if seconds:
            time.sleep(seconds)
        else:
            input("    recording... press Enter to stop ")
    finally:
        proc.terminate()
        proc.wait()
    length = time.time() - started
    tmp.replace(path) if tmp.stat().st_size > 1000 else None
    return length


def record_visitor(take: str, mic: str, redo: bool, seconds: float = 0.0) -> int:
    """The visitor's spoken lines of a take (the robot's microphone heard them; its sound was not kept): read each, in your own voice, at the
    moment it was said. They are saved as recordings/<take>/visitor/<n>.wav and mixed into the take's audio.wav (with Stuart's lines)."""
    from tools.video import session_to_take as stt
    folder = ROOT / "recordings" / take
    if not (folder / "events.jsonl").is_file():
        raise SystemExit(f"no take recordings/{take} (made with tools/video/session_to_take.py)")
    meta = __import__("json").loads((folder / "meta.json").read_text())
    events = [__import__("json").loads(l) for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = stt.visitor_lines(events)
    if not lines:
        raise SystemExit("the visitor said nothing aloud in this take (a typed line makes no sound)")
    print(f"microphone: {mic}\n{len(lines)} visitor line(s) in {take}. Say each one as a visitor would (a few words, natural pace); the first\n"
          f"Enter starts the recording, the second stops it. Stand-in voice is used for any line you skip.\n")
    for n, end, text in lines:
        target = folder / "visitor" / f"{n}.wav"
        if target.is_file() and not redo:
            print(f"[{n}/{len(lines)}] already recorded ({target.name}); --redo records it again")
            continue
        while True:
            print(f"[{n}/{len(lines)}] at {clock(end - meta['start_time'])} in the recording:\n\n    {text}\n")
            if not seconds and input("    Enter = start recording, s = skip: ").strip().lower() == "s":
                break
            length = record_one(target, mic, seconds)
            print(f"    {length:.1f} s saved")
            if seconds or input("    Enter = keep, r = record again: ").strip().lower() != "r":
                break
    print(f"\nmixed into {stt.mix_take_audio(folder)}")
    print("Render with: python tools/video/edit.py --preview")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="voiceover.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--storyboard", default=str(ROOT / "video" / "storyboard.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cues", help="the lines with their times and file names")
    c.add_argument("--write", action="store_true", help="write video/voiceover.md")
    r = sub.add_parser("record", help="record the lines one by one")
    r.add_argument("--only", action="append", help="only this segment id (repeatable)")
    r.add_argument("--mic", help="part of a microphone's name (list-mics)")
    r.add_argument("--redo", action="store_true", help="also the lines already recorded")
    r.add_argument("--seconds", type=float, default=0.0, help="record each line for this long without pressing Enter (for tests)")
    v = sub.add_parser("visitor", help="record what the visitor says in a take (the lines the robot's microphone heard)")
    v.add_argument("--take", required=True)
    v.add_argument("--mic", help="part of a microphone's name (list-mics)")
    v.add_argument("--redo", action="store_true", help="also the lines already recorded")
    v.add_argument("--seconds", type=float, default=0.0, help="record each line for this long without pressing Enter (for tests)")
    sub.add_parser("list-mics", help="the microphones PulseAudio sees")
    args = p.parse_args(argv)
    if args.cmd == "list-mics":
        print("\n".join(mic_sources()))
        return 0
    if args.cmd == "visitor":
        return record_visitor(args.take, pick_mic(args.mic), args.redo, args.seconds)
    settings, segments, rec_dir = load(Path(args.storyboard))
    rows = cue_rows(segments, rec_dir)
    if args.cmd == "cues":
        print(cue_sheet(rows))
        if args.write:
            (ROOT / "video" / "voiceover.md").write_text(script_md(rows), encoding="utf-8")
            print(f"\nwrote video/voiceover.md ({sum(r['words'] for r in rows)} words)")
        return 0
    mic = pick_mic(args.mic)
    todo = [r for r in rows if (not args.only or r["id"] in args.only) and (args.redo or args.only or not r["recorded"])]
    print(f"microphone: {mic}   (another one: --mic <part of its name>; `list-mics` shows them)\n{len(todo)} line(s) to record. Speak at a calm pace; each file goes to {rec_dir}/<id>.wav\n")
    for n, r in enumerate(todo, 1):
        while True:
            print(f"[{n}/{len(todo)}] {r['id']}   starts {clock(r['start'])}, {r['slot']:.1f} s available (about {r['estimate']:.1f} s at a calm pace)")
            print(f"\n    {r['text']}\n")
            if not args.seconds:
                answer = input("    Enter = start recording, s = skip this line: ").strip().lower()
                if answer == "s":
                    break
            target = rec_dir / f"{r['id']}.wav"
            length = record_one(target, mic, args.seconds)
            note = "" if length <= r["slot"] + 0.3 else f"  (longer than the {r['slot']:.1f} s it has: it will run into the next line)"
            print(f"    {length:.1f} s saved{note}")
            if silent(target):
                print(f"    NOTHING WAS HEARD from {mic}. This is not the microphone you speak into: `list-mics` shows them, then run again with "
                      f"--mic <part of its name>, for example --mic MV_USB for the USB microphone.")
                if args.seconds:
                    break
                if input("    Enter = record this line again, q = stop here: ").strip().lower() == "q":
                    return 1
                continue
            if args.seconds or input("    Enter = keep, r = record again: ").strip().lower() != "r":
                break
    print("\nDone. Render with: python tools/video/edit.py --preview")
    return 0


if __name__ == "__main__":
    sys.exit(main())
