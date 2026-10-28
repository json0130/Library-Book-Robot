#!/usr/bin/env python3
# One-time setup for the two USB microphones (left + right of the robot).
#
#   1. Finds the USB microphones (the camera mic and laptop mic are ignored).
#   2. CLAP TEST: you clap next to the LEFT mic, then next to the RIGHT mic,
#      so it learns which is which.
#   3. CALIBRATION: you make a steady sound straight IN FRONT of the robot,
#      so it can correct for one mic being more sensitive than the other.
#   4. Saves everything to ~/mic_config.json - by physical USB port, so it
#      still works if the card numbers change after a reboot.
#
# Run:  python3 setup_mics.py
# (Stop the noise logger / run scripts first - they use the mics.)

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

RATE = 16000
CONFIG = os.path.expanduser('~/mic_config.json')
SKIP_NAMES = ('ORBBEC', 'HDA', 'PCH')       # camera + laptop built-in mics


def usb_port_of(card):
    """Physical USB port of a sound card, e.g. '1-3.2', or None if not USB."""
    dev = os.path.realpath('/sys/class/sound/card%d/device' % card)
    if '/usb' not in dev:
        return None
    return os.path.basename(os.path.dirname(dev))


def card_name(card):
    try:
        with open('/proc/asound/cards') as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == str(card):
                    return line.split(' - ', 1)[-1].strip()
    except OSError:
        pass
    return 'card %d' % card


def capture_cards():
    """List of (card number, usb port, name) for USB capture devices."""
    out = subprocess.run(['arecord', '-l'], capture_output=True, text=True).stdout
    cards = []
    for line in out.splitlines():
        if line.startswith('card '):
            n = int(line.split()[1].rstrip(':'))
            if n not in [c[0] for c in cards]:
                cards.append((n, usb_port_of(n), card_name(n)))
    return cards


def card_for_port(port):
    """Current card number for a USB port (card numbers can change on reboot)."""
    for c in sorted(os.listdir('/sys/class/sound')):
        if c.startswith('card') and c[4:].isdigit():
            n = int(c[4:])
            if usb_port_of(n) == port:
                return n
    return None


def record_all(cards, seconds):
    """Record from all cards at the same time. Returns {card: int16 array}."""
    results = {}

    def rec(n):
        p = subprocess.run(['arecord', '-q', '-D', 'plughw:%d,0' % n, '-f', 'S16_LE',
                            '-r', str(RATE), '-c', '1', '-t', 'raw', '-d', str(seconds)],
                           capture_output=True)
        results[n] = np.frombuffer(p.stdout, np.int16).astype(np.float64)

    th = [threading.Thread(target=rec, args=(n,)) for n in cards]
    for t in th:
        t.start()
    for t in th:
        t.join()
    return results


def frame_db(x, frame=0.02):
    """Loudness (dB re full scale) of each 20 ms frame."""
    n = int(RATE * frame)
    k = len(x) // n
    if k == 0:
        return np.array([-96.0])
    rms = np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(axis=1)) + 1e-9
    return 20 * np.log10(rms / 32768.0)


def loud_level(x):
    """Level of the loudest moments (top 5% of frames) - good for claps."""
    d = frame_db(x)
    return float(np.mean(np.sort(d)[-max(1, len(d) // 20):]))


def countdown(msg, secs=3):
    print('\n' + msg)
    for i in range(secs, 0, -1):
        print('   %d...' % i)
        time.sleep(1)


def main():
    cards = [c for c in capture_cards()
             if c[1] and not any(s in c[2].upper() for s in SKIP_NAMES)]
    print('USB microphones found:')
    for n, port, name in cards:
        print('   card %d  USB port %-8s %s' % (n, port, name))
    if len(cards) < 2:
        print('\nNeed 2 USB microphones but found %d. Check they are plugged in '
              '(arecord -l).' % len(cards))
        sys.exit(1)
    nums = [c[0] for c in cards]

    # ---- clap test ----
    levels = {}
    for side in ('LEFT', 'RIGHT'):
        countdown('Get ready to clap 4-5 times right next to the %s mic '
                  '(about 20 cm away). Recording starts in:' % side)
        print('   >>> CLAP NOW (5 seconds) <<<')
        rec = record_all(nums, 5)
        levels[side] = {n: loud_level(rec[n]) for n in nums}
        for n in nums:
            print('      card %d: %.1f dB' % (n, levels[side][n]))

    left = max(nums, key=lambda n: levels['LEFT'][n] - levels['RIGHT'][n])
    right = max(nums, key=lambda n: levels['RIGHT'][n] - levels['LEFT'][n])
    margin_l = levels['LEFT'][left] - levels['LEFT'][right]
    margin_r = levels['RIGHT'][right] - levels['RIGHT'][left]
    print('\nLEFT  mic = card %d (USB port %s), %.1f dB louder for left claps'
          % (left, usb_port_of(left), margin_l))
    print('RIGHT mic = card %d (USB port %s), %.1f dB louder for right claps'
          % (right, usb_port_of(right), margin_r))
    if left == right or margin_l < 2 or margin_r < 2:
        print('\n!! The two mics did not clearly hear the claps differently (need > 2 dB).')
        print('!! Clap closer to each mic and run this again.')
        sys.exit(1)

    # ---- calibration: steady sound in front ----
    countdown('CALIBRATION: play steady music or talk continuously about 1 m straight '
              'IN FRONT of the robot (centred between the mics). Starts in:', 3)
    print('   >>> keep the sound going (6 seconds) <<<')
    rec = record_all([left, right], 6)
    dl, dr = frame_db(rec[left]), frame_db(rec[right])
    k = min(len(dl), len(dr))
    loud = (dl[:k] + dr[:k]) / 2 > np.percentile((dl[:k] + dr[:k]) / 2, 50)
    offset = float(np.median(dl[:k][loud] - dr[:k][loud])) if loud.any() else 0.0
    print('   left mic reads %.1f dB %s than the right one for the same sound'
          % (abs(offset), 'louder' if offset > 0 else 'quieter'))
    if abs(offset) > 12:
        print('!! That is a big difference - check both mics are uncovered and the '
              'same model. Saving anyway.')

    cfg = {
        'left':  {'usb_port': usb_port_of(left),  'name': card_name(left)},
        'right': {'usb_port': usb_port_of(right), 'name': card_name(right)},
        'offset_db': round(offset, 2),     # subtracted from left readings
        'created': time.strftime('%Y-%m-%d %H:%M'),
    }
    with open(CONFIG, 'w') as f:
        json.dump(cfg, f, indent=2)
    print('\nSaved %s:' % CONFIG)
    print(json.dumps(cfg, indent=2))
    print('\nDone. noise_logger.py will now use these two mics.')


if __name__ == '__main__':
    main()
