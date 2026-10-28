#!/usr/bin/env python3
# Logs how loud it is around the TurtleBot while it drives.
#
# TWO-MIC MODE (if ~/mic_config.json exists - made by setup_mics.py):
#   records the LEFT and RIGHT USB mics separately, so you can tell which
#   side of the robot a sound is coming from.
# CAMERA MODE (no config): uses the camera's built-in mic, as before.
#
# Saves time, robot position (x, y), overall noise, left noise, right noise
# and the left/right difference to a CSV file.
#
# Publishes:
#   /noise_level    overall loudness (dB)
#   /noise_left     left mic loudness (dB)
#   /noise_right    right mic loudness (dB)
#   /noise_balance  left minus right (dB): positive = louder on the LEFT,
#                                          negative = louder on the RIGHT
#   /noise_reading  all four at once: [overall, left, right, balance]
#
# Run:  python3 noise_logger.py
# Options:  _file:=my_run.csv     (change output file name)
#           _camera:=true         (ignore the USB mics, use the camera mic)
#           _side_db:=1.5         (difference needed to say LEFT/RIGHT)
#           _level:=peak          (default) loudest 50 ms in each half second -
#                                 catches claps and short sounds
#           _level:=average       average over each half second (steady noise)

import audioop
import csv
import json
import math
import os
import queue
import subprocess
import threading
import time

import rospy
from nav_msgs.msg import Odometry
from std_msgs.msg import Float32, Float32MultiArray

RATE = 16000        # samples per second
CHUNK_SEC = 0.5     # measure loudness every 0.5 seconds
FRAME_SEC = 0.05    # peak mode: loudest 50 ms piece of each half second
CONFIG = os.path.expanduser('~/mic_config.json')

pos = [0.0, 0.0]


def to_db(rms):
    # Loudness in dB relative to the mic's maximum (0 = loudest possible).
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -96.0


def usb_port_of(card):
    dev = os.path.realpath('/sys/class/sound/card%d/device' % card)
    return os.path.basename(os.path.dirname(dev)) if '/usb' in dev else None


def card_for_port(port):
    """Current card number for a USB port (card numbers can change on reboot)."""
    for c in os.listdir('/sys/class/sound'):
        if c.startswith('card') and c[4:].isdigit() and usb_port_of(int(c[4:])) == port:
            return int(c[4:])
    return None


def chunk_level(data, mode):
    """RMS of a chunk of 16-bit mono audio: whole-chunk average, or the
    loudest 50 ms frame ('peak' - so short sounds like claps aren't averaged away)."""
    if mode == 'average':
        return audioop.rms(data, 2)
    n = int(RATE * FRAME_SEC) * 2
    return max(audioop.rms(data[i:i + n], 2) for i in range(0, len(data) - n + 1, n))


def odom_cb(msg):
    pos[0] = msg.pose.pose.position.x
    pos[1] = msg.pose.pose.position.y


class MicStream:
    """Reads one mono mic in a background thread; .q gets one dB value per chunk."""

    def __init__(self, device, mode='peak'):
        self.device = device
        self.mode = mode
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            ['arecord', '-D', device, '-f', 'S16_LE', '-r', str(RATE),
             '-c', '1', '-t', 'raw', '-q'], stdout=subprocess.PIPE)
        self.nbytes = int(RATE * CHUNK_SEC) * 2
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            data = self.proc.stdout.read(self.nbytes)
            if len(data) < self.nbytes:
                self.q.put(None)
                return
            self.q.put(chunk_level(data, self.mode))

    def get(self):
        # the two USB mics have separate clocks - drop old chunks if one runs ahead
        while self.q.qsize() > 2:
            self.q.get_nowait()
        return self.q.get(timeout=3)

    def stop(self):
        self.proc.terminate()


def two_mic_source(cfg, mode='peak'):
    """Yields (rms_left, rms_right) from the two USB mics."""
    cl, cr = card_for_port(cfg['left']['usb_port']), card_for_port(cfg['right']['usb_port'])
    if cl is None or cr is None:
        raise RuntimeError('USB mic not found on port %s / %s - are they plugged into the '
                           'same USB ports as when you ran setup_mics.py?'
                           % (cfg['left']['usb_port'], cfg['right']['usb_port']))
    rospy.loginfo('Two-mic mode: LEFT = card %d (port %s), RIGHT = card %d (port %s), '
                  'offset %.1f dB', cl, cfg['left']['usb_port'], cr,
                  cfg['right']['usb_port'], cfg.get('offset_db', 0.0))
    L, R = MicStream('plughw:%d,0' % cl, mode), MicStream('plughw:%d,0' % cr, mode)
    try:
        while not rospy.is_shutdown():
            a, b = L.get(), R.get()
            if a is None or b is None:
                rospy.logerr('A microphone stopped sending audio (unplugged?)')
                return
            yield a, b
    finally:
        L.stop()
        R.stop()


def camera_source(device, mode='peak'):
    """Yields (rms_left, rms_right) from the camera's stereo mic."""
    rospy.loginfo('Camera-mic mode (%s)', device)
    proc = subprocess.Popen(['arecord', '-D', device, '-f', 'S16_LE', '-r', str(RATE),
                             '-c', '2', '-t', 'raw', '-q'], stdout=subprocess.PIPE)
    nbytes = int(RATE * CHUNK_SEC) * 2 * 2
    try:
        while not rospy.is_shutdown():
            data = proc.stdout.read(nbytes)
            if len(data) < nbytes:
                return
            yield (chunk_level(audioop.tomono(data, 2, 1, 0), mode),
                   chunk_level(audioop.tomono(data, 2, 0, 1), mode))
    finally:
        proc.terminate()


def main():
    rospy.init_node('noise_logger')
    out_file = rospy.get_param('~file', 'noise_log.csv')
    force_camera = str(rospy.get_param('~camera', False)).lower() in ('true', '1', 'yes')
    side_db = float(rospy.get_param('~side_db', 1.5))
    mode = str(rospy.get_param('~level', 'peak')).lower()
    if mode not in ('peak', 'average'):
        mode = 'peak'
    rospy.loginfo('Measuring %s loudness', mode)

    rospy.Subscriber('/odom', Odometry, odom_cb)
    pub_all = rospy.Publisher('/noise_level', Float32, queue_size=10)
    pub_l = rospy.Publisher('/noise_left', Float32, queue_size=10)
    pub_r = rospy.Publisher('/noise_right', Float32, queue_size=10)
    pub_bal = rospy.Publisher('/noise_balance', Float32, queue_size=10)
    pub_all4 = rospy.Publisher('/noise_reading', Float32MultiArray, queue_size=10)

    offset = 0.0
    if os.path.exists(CONFIG) and not force_camera:
        with open(CONFIG) as f:
            cfg = json.load(f)
        offset = float(cfg.get('offset_db', 0.0))
        source = two_mic_source(cfg, mode)
    else:
        if not force_camera:
            rospy.logwarn('No %s - using the camera mic. Run setup_mics.py to use the '
                          'left/right USB mics.', CONFIG)
        cam = None
        for c in os.listdir('/sys/class/sound'):
            if c.startswith('card') and c[4:].isdigit():
                try:
                    if 'ORBBEC' in open('/proc/asound/%s/id' % c).read().upper() or \
                       'SENSOR' in open('/proc/asound/%s/id' % c).read().upper():
                        cam = int(c[4:])
                except OSError:
                    pass
        source = camera_source('plughw:%d,0' % cam if cam is not None else 'plughw:1,0', mode)

    rospy.loginfo('Logging noise to %s  (Ctrl-C to stop)', out_file)
    with open(out_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['time_s', 'x_m', 'y_m', 'noise_dB',
                         'left_dB', 'right_dB', 'balance_dB'])
        t0 = time.time()
        try:
            for rms_l, rms_r in source:
                db_l = to_db(rms_l) - offset          # correct for mic sensitivity
                db_r = to_db(rms_r)
                # overall = average sound power of both mics
                p = (10 ** (db_l / 10) + 10 ** (db_r / 10)) / 2
                db_all = 10 * math.log10(p) if p > 0 else -96.0
                balance = db_l - db_r

                pub_all.publish(db_all)
                pub_l.publish(db_l)
                pub_r.publish(db_r)
                pub_bal.publish(balance)
                pub_all4.publish(Float32MultiArray(data=[db_all, db_l, db_r, balance]))

                writer.writerow([round(time.time() - t0, 2),
                                 round(pos[0], 3), round(pos[1], 3), round(db_all, 1),
                                 round(db_l, 1), round(db_r, 1), round(balance, 1)])
                f.flush()

                side = 'LEFT' if balance > side_db else ('RIGHT' if balance < -side_db else 'centre')
                rospy.loginfo('x=%.2f y=%.2f  noise=%.1f dB  L=%.1f R=%.1f  -> %s',
                              pos[0], pos[1], db_all, db_l, db_r, side)
        except RuntimeError as e:
            rospy.logerr(str(e))
        except queue.Empty:
            rospy.logerr('No audio from a microphone for 3 s (unplugged or in use?)')
    rospy.loginfo('Saved %s', out_file)


if __name__ == '__main__':
    main()
