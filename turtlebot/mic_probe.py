#!/usr/bin/env python3
"""Does this microphone follow the room? A 40-second test of ONE USB microphone, without ROS and without the noise logger.

    python3 mic_probe.py --port 1-1          # the microphone on that USB port (see `arecord -l`, or setup_mics.py's list)
    python3 mic_probe.py --card 4            # or by card number
    python3 mic_probe.py --port 1-1 --phone  # also ask what a phone sound-meter app shows at each step

It records three steps of 10 s (quiet room, steady talking-level sound, louder steady sound), measures each the way noise_logger.py does (the loudest
50 ms piece of every half second, in dBFS), and says whether the microphone's reading rises with the sound or sits on its own noise floor.
On 2026-11-18 the two USB microphones on this robot both failed that test: they read the same for a quiet room and for a 60 dB sound.

It only listens (arecord). It changes nothing: no mixer settings, no files, no robot topics. Leave the robot still and do not speak during the quiet step.
"""

import argparse
import os
import statistics
import struct
import subprocess
import sys
import math

try:
    import audioop
except ImportError:                                       # Python 3.13 has no audioop
    audioop = None

RATE = 16000
CHUNK = 0.5          # seconds per reading, like noise_logger.py
FRAME = 0.05         # the loudest 50 ms of each chunk


def rms(data):
    if audioop:
        return audioop.rms(data, 2)
    n = len(data) // 2
    s = struct.unpack("<%dh" % n, data[:n * 2])
    return math.sqrt(sum(x * x for x in s) / n) if n else 0.0


def db(r):
    return 20 * math.log10(r / 32768.0) if r > 0 else -96.0


def peak_level(chunk):
    n = int(RATE * FRAME) * 2
    return max(rms(chunk[i:i + n]) for i in range(0, len(chunk) - n + 1, n))


def card_of_port(port):
    for c in os.listdir("/sys/class/sound"):
        if c.startswith("card") and c[4:].isdigit():
            dev = os.path.realpath("/sys/class/sound/%s/device" % c)
            if "/usb" in dev and os.path.basename(os.path.dirname(dev)) == port:
                return int(c[4:])
    return None


def record(card, seconds):
    """[dBFS per half second] from one card."""
    nbytes = int(RATE * CHUNK) * 2
    proc = subprocess.Popen(["arecord", "-D", "plughw:%d,0" % card, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-q"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    try:
        for _ in range(int(seconds / CHUNK)):
            data = proc.stdout.read(nbytes)
            if len(data) < nbytes:
                raise RuntimeError("the microphone stopped sending audio (arecord said nothing more)")
            out.append(db(peak_level(data)))
    finally:
        proc.terminate()
    return out[2:] if len(out) > 6 else out                # the first second is the start-up


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="USB port of the microphone, for example 1-1 or 1-2.6")
    g.add_argument("--card", type=int, help="ALSA card number (arecord -l)")
    ap.add_argument("--seconds", type=float, default=10.0, help="length of each step (default 10)")
    ap.add_argument("--phone", action="store_true", help="ask what the phone's sound meter shows at each step")
    args = ap.parse_args(argv)
    card = args.card if args.card is not None else card_of_port(args.port)
    if card is None:
        print("no microphone on USB port %s (arecord -l lists them)" % args.port)
        return 2
    steps = [("QUIET", "nothing playing, nobody talking, no typing"),
             ("MEDIUM", "a steady sound at talking level: a tone or recording from a speaker about 30 cm from the microphone (about 60 dB on a phone)"),
             ("LOUD", "the same speaker louder, about 70-75 dB on a phone, not painful")]
    results = []
    for name, how in steps:
        input("\n%s: %s.\n  Press Enter, then keep it steady for %g s ... " % (name, how, args.seconds))
        try:
            levels = record(card, args.seconds)
        except (RuntimeError, OSError) as exc:
            print("  %s" % exc)
            return 2
        med = statistics.median(levels)
        ordered = sorted(levels)
        spread = ordered[int(len(ordered) * 0.9) - 1] - ordered[int(len(ordered) * 0.1)]       # without the extreme tenths: one click does not count
        phone = None
        if args.phone:
            try:
                phone = float(input("  phone shows (dB, Enter to skip): ") or "nan")
            except ValueError:
                phone = None
        results.append((name, med, spread, phone))
        print("  %s: median %.1f dBFS, moved by %.1f dB%s" % (name, med, spread, "  (not steady: repeat this step)" if spread > 6 else ""))
    q, m, l = (r[1] for r in results)
    print("\nstep      dBFS     above the quiet room")
    for name, med, _, phone in results:
        print("%-8s %6.1f   %+5.1f dB%s" % (name, med, med - q, "   phone %.0f" % phone if phone is not None and phone == phone else ""))
    print()
    if m - q < 6:
        print("VERDICT: NO. The medium sound reads only %+.1f dB above the quiet room: the microphone sits on its own noise floor (or the sound did not reach it)." % (m - q))
    elif l - m < 6:
        print("VERDICT: PARTLY. It hears the medium sound (%+.1f dB) but the louder one adds only %+.1f dB: it may be near its limit, or the step was not louder." % (m - q, l - m))
    else:
        print("VERDICT: YES. It rises with the sound (%+.1f dB, then %+.1f dB more). It can map differences between places." % (m - q, l - m))
        print("  For real decibels, run it again with --phone and check the steps follow the phone: about the same difference per step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
