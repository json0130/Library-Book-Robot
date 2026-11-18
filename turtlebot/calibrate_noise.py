#!/usr/bin/env python3
# Noise calibration: turns the microphones' dBFS into a real sound level in dB, using a phone sound meter as the reference.
#
# Why: noise_logger.py reports dBFS (decibels below the microphone's full scale, always negative). The Jetson decides "quiet
# zone / common zone" from dB thresholds (about 48 / 55 dB), so until this has been done the API reports `unit: dBFS,
# calibrated: false` and the Jetson ignores the noise.
#
# What it does:
#   1. checks the microphones' gain and automatic gain control (AGC) with amixer and prints the commands to fix what is wrong.
#      The gain must stay exactly as it is during the calibration AND afterwards, or the calibration is wrong.
#   2. guided, three sound levels: you make a steady sound, hold the phone's sound meter app next to the robot's microphones,
#      type what the phone shows. The script measures both microphones for 10 s each time.
#   3. fits ONE offset per microphone (dB = dBFS + offset), reports the error at each level, and saves ~/noise_calibration.json.
#      robot_link_api.py and noise_logger.py pick the file up by themselves within a few seconds.
#
# One microphone only: a mono config (`python3 setup_mics.py --mono --port 1-1`) makes left and right the same microphone; the table then
# shows the same numbers twice.
#
# Needs ROS running with noise_logger.py (run_robot_link.sh starts it). The robot must be standing still and quiet.
#
# Run:     python3 calibrate_noise.py
# Options: --seconds 10        how long to measure each level
#          --check             only the gain / AGC check, change nothing
#          --tolerance 3.0     the largest error (dB) accepted without --force
#          --force             save even if the error is larger
#          --file PATH         where to save (default ~/noise_calibration.json)

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from robot_link import calibration                                        # noqa: E402

MIC_CONFIG = os.path.expanduser(os.environ.get('MIC_CONFIG') or '~/mic_config.json')      # the same file noise_logger.py reads
LEVELS = [
    ('QUIET', 'a quiet room: nothing playing, nobody talking (about 35-45 dB on the phone)'),
    ('MEDIUM', 'steady noise at talking level: music or a recording from a speaker about 1 m away (about 55-65 dB)'),
    ('LOUD', 'loud: the same speaker turned up (about 70-80 dB), but not so loud that it hurts'),
]


# ---------------------------------------------------------------------------------------------- the mixer (gain, AGC)
def card_for_port(port):
    """ALSA card number of the USB device on a USB port (the same lookup as noise_logger.py)."""
    for c in os.listdir('/sys/class/sound'):
        if c.startswith('card') and c[4:].isdigit():
            dev = os.path.realpath('/sys/class/sound/%s/device' % c)
            if '/usb' in dev and os.path.basename(os.path.dirname(dev)) == port:
                return int(c[4:])
    return None


def cards_from_config(cfg, find=card_for_port):
    """{name: card or None} of the microphones in a mic_config.json: left and right, or the one `microphone` of a mono config."""
    if 'mono' in cfg:
        return {'microphone': find(cfg['mono']['usb_port'])}
    return {side: find(cfg[side]['usb_port']) for side in ('left', 'right')}


def parse_mixer(text):
    """{'volume_pct': 100, 'capture_on': True, 'agc': False} from `amixer -c N scontents`; missing controls are None."""
    out = {'volume_pct': None, 'capture_on': None, 'agc': None}
    for block in re.split(r"(?=Simple mixer control)", text):
        name = re.match(r"Simple mixer control '([^']*)'", block)
        if not name:
            continue
        if name.group(1) == 'Auto Gain Control':
            m = re.search(r'\[(on|off)\]', block)
            out['agc'] = None if not m else m.group(1) == 'on'
        elif 'cvolume' in block:
            m = re.search(r'\[(\d+)%\]', block)
            out['volume_pct'] = int(m.group(1)) if m else None
            m = re.search(r'\[(on|off)\]', block)
            out['capture_on'] = None if not m else m.group(1) == 'on'
            out['control'] = name.group(1)
    return out


def mixer_advice(card, state):
    """(problems, commands) for one microphone's mixer state."""
    problems, commands = [], []
    if state.get('agc') is True:
        problems.append('automatic gain control is ON: it changes the gain with the loudness, which makes any calibration wrong')
        commands.append("amixer -c %d cset name='Auto Gain Control' off" % card)
    if state.get('capture_on') is False:
        problems.append('the microphone is muted')
        commands.append("amixer -c %d cset name='Mic Capture Switch' on" % card)
    if state.get('volume_pct') is not None and state['volume_pct'] >= 100:
        problems.append('the capture volume is at 100%: a loud sound can clip (distort), and the reading stops following the sound. '
                        'Lower it once and leave it there, e.g. 70%')
        commands.append("amixer -c %d sset '%s' 70%%" % (card, state.get('control', 'Mic')))
    return problems, commands


def check_mixers(cards, say=print):
    """Reports gain / AGC for each microphone. Returns ({side: state}, True if everything is fixed and sensible)."""
    states, ok = {}, True
    for side, card in cards.items():
        try:
            text = subprocess.run(['amixer', '-c', str(card), 'scontents'], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            say('  %s microphone (card %d): cannot run amixer: %s' % (side, card, exc))
            ok = False
            continue
        state = parse_mixer(text)
        states[side] = state
        problems, commands = mixer_advice(card, state)
        say('  %s microphone (card %d): capture volume %s%%, AGC %s' % (
            side, card, state['volume_pct'], {True: 'ON', False: 'off', None: 'not present'}[state['agc']]))
        for problem in problems:
            say('    ! ' + problem)
        for command in commands:
            say('      run:  ' + command)
        ok = ok and not [p for p in problems if 'AGC' in p or 'automatic' in p or 'muted' in p]
    return states, ok


# ---------------------------------------------------------------------------------------------- the measurements
def collect_ros(seconds):
    """Medians of /noise_reading (dBFS) over `seconds`: {'overall', 'left', 'right', 'spread', 'n'}."""
    import rospy
    from std_msgs.msg import Float32MultiArray
    rows = []
    sub = rospy.Subscriber('/noise_reading', Float32MultiArray, lambda m: rows.append(list(m.data[:3])), queue_size=50)
    try:
        end = time.time() + seconds
        while time.time() < end and not rospy.is_shutdown():
            time.sleep(0.1)
    finally:
        sub.unregister()
    return summarise(rows)


def summarise(rows):
    if len(rows) < 4:
        raise RuntimeError('only %d readings arrived on /noise_reading: is noise_logger.py running?' % len(rows))
    cols = list(zip(*rows))
    spread = max(max(c) - min(c) for c in cols)
    return {'overall': statistics.median(cols[0]), 'left': statistics.median(cols[1]), 'right': statistics.median(cols[2]),
            'spread': spread, 'n': len(rows)}


def run(collect, ask, say=print, seconds=10.0, tolerance=3.0, force=False):
    """The guided session. collect(seconds) -> summary; ask(prompt) -> text. Returns the fit (see calibration.fit) or None."""
    points = []
    say('\nThree sound levels. Keep the robot still, do not speak near it, and hold the phone right next to the microphones.\n')
    for name, how in LEVELS:
        say('Level %d of %d - %s: %s' % (len(points) + 1, len(LEVELS), name, how))
        while True:
            ask('  Make the sound, wait a moment until it is steady, and press Enter to measure for %g s ...' % seconds)
            m = collect(seconds)
            note = ' (it moved by %.1f dB: make it steadier)' % m['spread'] if m['spread'] > 4 else ''
            say('  measured %d readings: left %.1f dBFS, right %.1f dBFS%s' % (m['n'], m['left'], m['right'], note))
            text = ask('  What does the phone show (dB, the steady value)? Enter = measure again: ').strip()
            if not text:
                continue
            try:
                ref = float(text)
            except ValueError:
                say('  that is not a number')
                continue
            if not 20 <= ref <= 120:
                say('  a sound level between 20 and 120 dB, please')
                continue
            points.append({'reference': ref, 'left': m['left'], 'right': m['right'], 'overall': m['overall']})
            break
        say('')
    result = calibration.fit(points, tolerance)
    say('Result  (dB = dBFS + offset):')
    say('  left  offset %+.2f dB  (slope %.2f)' % (result['left_offset_db'], result['left_slope']))
    say('  right offset %+.2f dB  (slope %.2f)' % (result['right_offset_db'], result['right_slope']))
    say('  level       phone     robot   error')
    for r in result['points']:
        say('  %-9s %6.1f  %8.1f  %+6.1f dB' % ('', r['reference'], r['overall'], r['error_db']))
    say('  largest error %.1f dB, rms %.1f dB' % (result['max_error_db'], result['rms_error_db']))
    for w in result['warnings']:
        say('  ! ' + w)
    result['accepted'] = result['max_error_db'] <= tolerance or force
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description='Calibrate the noise microphones against a phone sound meter.')
    p.add_argument('--seconds', type=float, default=10.0)
    p.add_argument('--tolerance', type=float, default=3.0)
    p.add_argument('--check', action='store_true', help='only check the gain and AGC')
    p.add_argument('--force', action='store_true')
    p.add_argument('--file', default=calibration.DEFAULT_PATH)
    args = p.parse_args(argv)

    try:
        with open(MIC_CONFIG) as f:
            cfg = json.load(f)
        cards = cards_from_config(cfg)
    except (OSError, ValueError, KeyError):
        print('%s is missing or wrong: run setup_mics.py first (it says which microphone is left and which is right).' % MIC_CONFIG)
        return 1
    missing = [s for s, c in cards.items() if c is None]
    if missing:
        print('The %s microphone is not plugged into the USB port saved in mic_config.json.' % ' and '.join(missing))
        return 1

    print('Microphone gain check:')
    mixer, ok = check_mixers(cards)
    if args.check:
        try:
            saved = calibration.load(args.file)
        except calibration.CalibrationError as exc:
            print('\n! %s' % exc)
            saved = None
        if saved is None:
            print('\nNo calibration saved yet (%s).' % args.file)
        for side, state in mixer.items():
            before = (saved.mixer.get(side) or {}) if saved else {}
            if before and before.get('volume_pct') != state.get('volume_pct'):
                print('\n! the %s microphone\'s volume was %s%% when calibrated and is %s%% now: calibrate again.'
                      % (side, before.get('volume_pct'), state.get('volume_pct')))
                ok = False
        return 0 if ok else 1
    if not ok:
        print('\nFix the gain first (commands above), then run this again. Nothing was changed.')
        return 1

    import rospy
    rospy.init_node('calibrate_noise', anonymous=True, disable_signals=True)
    level_mode = str(rospy.get_param('/noise_logger/level', 'peak'))
    print('\nThe noise logger measures the %s level of each half second. The phone shows a steadier average, so for steady sounds '
          'the two agree.' % level_mode)
    try:
        result = run(lambda s: collect_ros(s), lambda prompt: input(prompt), print, args.seconds, args.tolerance, args.force)
    except (RuntimeError, ValueError) as exc:
        print('\nStopped: %s' % exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled. Nothing was saved.')
        return 1
    if not result['accepted']:
        print('\nNot saved: the error is above %.1f dB. Fix the cause (see above) and run again, or use --force.' % args.tolerance)
        return 1
    calibration.save(args.file, result, level_mode, mixer)
    print('\nSaved %s (the old file, if any, is %s.bak). The API and the logger use it within a few seconds.' % (args.file, args.file))
    print('Do not change the microphones\' volume afterwards: --check compares with what was saved here.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
