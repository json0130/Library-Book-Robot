#!/usr/bin/env python3
# Makes the TurtleBot drive around by itself using the Hokuyo LiDAR.
# It drives forward, steers away from walls, and turns towards open space
# when something is in front of it. gmapping draws the map while it drives.
#
# SAFETY:
#  - Pressing keys in keyboard teleop OVERRIDES this script (teleop has higher
#    priority), so keep the teleop window open to take control.
#  - If the bumper hits something, it backs up and turns away.
#  - Ctrl-C stops the robot.
#
# Run:  python3 auto_explore.py
# Options:  _speed:=0.15        forward speed (m/s), keep it slow
#           _safe_dist:=0.6     turn when something is closer than this (m)
#           _duration:=300      stop by itself after this many seconds (0 = never)
#           _front_offset:=3.14159   if the LiDAR is mounted facing backwards

import math
import random

FRONT = (-0.52, 0.52)    # front = within +/- 30 degrees
LEFT = (0.52, 1.57)      # left  = 30 to 90 degrees
RIGHT = (-1.57, -0.52)   # right = -90 to -30 degrees
CAP = 3.0                # treat anything further than 3 m as "open"


def sector_min(ranges, angle_min, inc, lo, hi, offset=0.0, rmin=0.03, rmax=5.6):
    """Closest valid LiDAR distance between angles lo and hi (radians)."""
    best = CAP
    for i, r in enumerate(ranges):
        if r is None or math.isnan(r) or r <= rmin or r >= rmax:
            continue
        a = angle_min + i * inc + offset
        a = math.atan2(math.sin(a), math.cos(a))
        if lo <= a <= hi and r < best:
            best = r
    return best


class Explorer:
    """Simple 'wander and avoid' brain. step() returns (linear, angular)."""

    def __init__(self, speed=0.15, safe=0.6, turn_speed=0.6):
        self.speed, self.safe, self.turn_speed = speed, safe, turn_speed
        self.mode = 'forward'
        self.turn_dir = 1
        self.mode_start = 0.0
        self.extra_turn = 0.0

    def bumped(self, now):
        self.mode = 'backup'
        self.mode_start = now

    def step(self, front, left, right, now):
        if self.mode == 'backup':
            if now - self.mode_start < 1.0:
                return -0.08, 0.0
            self._start_turn(left, right, now)

        if self.mode == 'forward':
            if front < self.safe:
                self._start_turn(left, right, now)
            else:
                # slow down near obstacles, steer away from the closer side
                lin = self.speed if front > self.safe + 0.5 else self.speed * 0.5
                ang = max(-0.4, min(0.4, 0.5 * (left - right) / CAP))
                if min(left, right) < 0.35:   # very close to a side wall
                    ang = 0.5 if left > right else -0.5
                return lin, ang

        # turning on the spot
        clear = front > self.safe + 0.25
        turned_long = now - self.mode_start > 12.0
        if turned_long:                       # stuck turning - try the other way
            self.turn_dir *= -1
            self.mode_start = now
        if clear and now - self.mode_start > self.extra_turn:
            self.mode = 'forward'
            self.mode_start = now
            return 0.0, 0.0
        return 0.0, self.turn_dir * self.turn_speed

    def _start_turn(self, left, right, now):
        self.mode = 'turning'
        self.mode_start = now
        if abs(left - right) < 0.2:
            self.turn_dir = random.choice((-1, 1))
        else:
            self.turn_dir = 1 if left > right else -1
        # a little random extra turn so it doesn't repeat the same path
        self.extra_turn = random.uniform(0.3, 1.5)


def main():
    import rospy
    from sensor_msgs.msg import LaserScan
    from geometry_msgs.msg import Twist

    rospy.init_node('auto_explore')
    speed = float(rospy.get_param('~speed', 0.15))
    safe = float(rospy.get_param('~safe_dist', 0.6))
    duration = float(rospy.get_param('~duration', 0))
    offset = float(rospy.get_param('~front_offset', 0.0))
    scan_topic = rospy.get_param('~scan', '/hokuyo_scan')
    cmd_topic = rospy.get_param('~cmd', '/cmd_vel_mux/input/navi')

    brain = Explorer(speed, safe)
    latest = {'scan': None, 'time': 0.0}

    def scan_cb(msg):
        latest['scan'] = msg
        latest['time'] = rospy.get_time()

    rospy.Subscriber(scan_topic, LaserScan, scan_cb)
    pub = rospy.Publisher(cmd_topic, Twist, queue_size=1)

    # Bumper (Kobuki base) - optional
    try:
        from kobuki_msgs.msg import BumperEvent

        def bump_cb(msg):
            if msg.state == BumperEvent.PRESSED:
                rospy.logwarn('Bumper hit - backing up')
                brain.bumped(rospy.get_time())
        rospy.Subscriber('/mobile_base/events/bumper', BumperEvent, bump_cb)
    except ImportError:
        rospy.logwarn('kobuki_msgs not found - bumper safety is OFF')

    def stop():
        pub.publish(Twist())

    rospy.on_shutdown(stop)
    rospy.loginfo('Auto explore started on %s  (Ctrl-C to stop)', scan_topic)

    start = rospy.get_time()
    rate = rospy.Rate(10)
    last_log = 0.0
    while not rospy.is_shutdown():
        now = rospy.get_time()
        if duration > 0 and now - start > duration:
            rospy.loginfo('Time is up - stopping')
            break

        s = latest['scan']
        cmd = Twist()
        if s is None or now - latest['time'] > 1.0:
            if now - last_log > 3:
                rospy.logwarn('No LiDAR data on %s - waiting (robot stopped)', scan_topic)
                last_log = now
        else:
            args = (s.ranges, s.angle_min, s.angle_increment)
            kw = dict(offset=offset, rmin=max(s.range_min, 0.03), rmax=s.range_max)
            front = sector_min(*args, *FRONT, **kw)
            left = sector_min(*args, *LEFT, **kw)
            right = sector_min(*args, *RIGHT, **kw)
            cmd.linear.x, cmd.angular.z = brain.step(front, left, right, now)
            if now - last_log > 2:
                rospy.loginfo('%-8s front=%.2f left=%.2f right=%.2f',
                              brain.mode, front, left, right)
                last_log = now
        pub.publish(cmd)
        rate.sleep()

    stop()


if __name__ == '__main__':
    main()
