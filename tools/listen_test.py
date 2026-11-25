"""Check the microphone for speech: how loud is the room, how loud is your voice, and does the robot understand you?

    python -m tools.listen_test            # the microphone in config/robot.yaml (devices.mic), the AI server in ai_server
    python -m tools.listen_test --no-stt   # only measure the levels

Three steps: 3 s of quiet (the room), then you say a sentence (5 s), then the real listener runs once, exactly as the Talk
button does, and prints what the AI server heard. It ends with the `listen.min_rms` to put in config/robot.yaml.
It records only: it changes no settings and sends the recording to the AI server only in the last step.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import closing

from robot.bus import Bus
from robot.config import load_config
from robot.perception.listener import CHUNK_S, Listener, rms


def levels(audio, seconds: float) -> list:
    out = []
    with closing(audio.stream(CHUNK_S)) as chunks:
        for chunk in chunks:
            out.append(rms(chunk))
            if len(out) >= seconds / CHUNK_S:
                break
    return out


def pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-stt", action="store_true", help="only measure the levels; do not contact the AI server")
    args = ap.parse_args(argv)
    cfg = load_config()
    from robot.drivers.audio import Audio
    try:
        audio = Audio(None, cfg.robot["devices"].get("mic"))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cannot open the microphone: {exc}")
        return 2
    print(f"microphone: {audio.mic or 'the system default'}\n")
    input("Step 1: stay quiet. Press Enter, then say nothing for 3 s ... ")
    room = levels(audio, 3.0)
    if len(room) < 10:
        print("The microphone gave no sound: is it unplugged, or muted (pactl list short sources)?")
        return 2
    noise = pct(room, 0.9)
    print(f"  room: typically {pct(room, 0.5):.0f}, loudest pieces {noise:.0f}")
    input("\nStep 2: Press Enter, then say a sentence at the volume a visitor would use, from where they would stand (5 s) ... ")
    voice = levels(audio, 5.0)
    talk = pct(voice, 0.75)
    print(f"  voice: typically {pct(voice, 0.5):.0f}, the louder pieces {talk:.0f}, peak {max(voice, default=0):.0f}")
    if talk < 2 * noise:
        print(f"\nYour voice is not clearly louder than the room ({talk:.0f} against {noise:.0f}). Move closer, speak up, or raise the microphone's"
              " volume (pactl set-source-volume <source> 100%), then run this again.")
        return 1
    advice = int(round((noise * 1.5 + talk * 0.25) / 10.0) * 10)
    advice = max(advice, int(noise * 1.3))
    print(f"\n=> set listen.min_rms in config/robot.yaml to about {advice} (now {cfg.robot.get('listen', {}).get('min_rms')}). "
          f"The robot will also ignore anything under 3x the room's noise when listening starts ({3 * noise:.0f} now).")
    if args.no_stt:
        return 0
    from robot.services.speech import Speech
    speech = Speech.from_config(cfg)
    problem = speech.check()
    if problem:
        print(f"\n{problem}")
        return 1
    input("\nStep 3: Press Enter, then say a sentence, and stop. The robot listens like the Talk button does ... ")
    listener = Listener.from_config(cfg, audio, speech, Bus(), say=print)
    listener.min_rms = float(advice)
    t0 = time.time()
    text = asyncio.run(listener.listen())
    print(f"\n{'the robot heard: ' + repr(text) if text else 'nothing understood'} ({time.time() - t0:.1f} s in all)")
    return 0 if text else 1


if __name__ == "__main__":
    sys.exit(main())
