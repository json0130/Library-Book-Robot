#!/usr/bin/env python3
# Tells AMCL where the robot starts: the station pose from data/floorplan.yaml (`station: {level, x, y, yaw}`, the same
# pose the Jetson sends the robot home to), published once to /initialpose. Started by run_robot_link.sh --amcl after
# move_base is up; it waits for AMCL to listen, publishes, and waits for AMCL's first /amcl_pose to confirm.
#
# The robot must really be at the station, facing the station's way. If it is not, AMCL starts from the wrong place:
# use "Set robot position" on the click-to-go page, or put it back on the station and run this again.
#
# Run:  python3 nodes/set_initial_pose.py                        station from ../data/floorplan.yaml
#       python3 nodes/set_initial_pose.py --pose 1.0,2.0,1.57    x, y (m) and yaw (rad) in the map frame instead
#       python3 nodes/set_initial_pose.py --floorplan FILE --xy-sigma 0.15 --yaw-sigma-deg 8 --wait 30
# Exit status: 0 AMCL took the pose, 1 it did not answer in time, 2 no usable pose.

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FLOORPLAN = os.path.join(HERE, "..", "..", "data", "floorplan.yaml")


def station_pose(path):
    """(x, y, yaw) of the `station:` in a floor plan. Raises ValueError with a message the user can act on."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (path, exc))
    except yaml.YAMLError as exc:
        raise ValueError("%s is not valid YAML: %s" % (path, exc))
    raw = data.get("station") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("%s has no station: {level, x, y, yaw}" % path)
    try:
        pose = (float(raw["x"]), float(raw["y"]), float(raw.get("yaw", 0.0)))
    except (KeyError, TypeError, ValueError):
        raise ValueError("the station in %s needs x and y (numbers)" % path)
    if not all(math.isfinite(v) for v in pose):
        raise ValueError("the station in %s is not a finite pose" % path)
    return pose


def parse_pose(text):
    """'x,y,yaw' -> (x, y, yaw)."""
    try:
        values = tuple(float(v) for v in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        raise ValueError("--pose needs x,y,yaw (three numbers, metres and radians), not %r" % text)
    return values


def covariance(xy_sigma_m, yaw_sigma_rad):
    """The 6x6 row-major covariance of a PoseWithCovariance: x, y and yaw variances, nothing else."""
    c = [0.0] * 36
    c[0] = c[7] = xy_sigma_m ** 2
    c[35] = yaw_sigma_rad ** 2
    return c


def main(argv=None):
    p = argparse.ArgumentParser(description="Send the station pose to AMCL (/initialpose) once.")
    p.add_argument("--floorplan", default=DEFAULT_FLOORPLAN, help="the floor plan whose station: is used")
    p.add_argument("--pose", help="x,y,yaw in the map frame instead of the station")
    p.add_argument("--xy-sigma", type=float, default=0.15, help="how far off the robot may be standing, m (default 0.15)")
    p.add_argument("--yaw-sigma-deg", type=float, default=8.0, help="how far off it may be turned, degrees (default 8)")
    p.add_argument("--wait", type=float, default=30.0, help="seconds to wait for AMCL (default 30)")
    p.add_argument("--topic", default="/initialpose")
    p.add_argument("--confirm-topic", default="/amcl_pose")
    args = p.parse_args(argv)
    try:
        x, y, yaw = parse_pose(args.pose) if args.pose else station_pose(args.floorplan)
    except ValueError as exc:
        print("set_initial_pose: %s" % exc, file=sys.stderr)
        return 2

    import rospy
    from geometry_msgs.msg import PoseWithCovarianceStamped
    rospy.init_node("set_initial_pose", anonymous=True, disable_signals=True)
    pub = rospy.Publisher(args.topic, PoseWithCovarianceStamped, queue_size=1, latch=True)
    confirmed = []
    rospy.Subscriber(args.confirm_topic, PoseWithCovarianceStamped, lambda m: confirmed.append(m), queue_size=1)
    end = time.time() + args.wait
    while pub.get_num_connections() == 0 and time.time() < end:
        time.sleep(0.2)
    if pub.get_num_connections() == 0:
        print("set_initial_pose: nothing listens on %s after %g s (is AMCL running?)" % (args.topic, args.wait), file=sys.stderr)
        return 1
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = "map"
    msg.header.stamp = rospy.Time.now()
    msg.pose.pose.position.x, msg.pose.pose.position.y = x, y
    msg.pose.pose.orientation.z, msg.pose.pose.orientation.w = math.sin(yaw / 2.0), math.cos(yaw / 2.0)
    msg.pose.covariance = covariance(args.xy_sigma, math.radians(args.yaw_sigma_deg))
    pub.publish(msg)
    print("set_initial_pose: sent x %.2f  y %.2f  yaw %.2f rad to %s" % (x, y, yaw, args.topic))

    def taken():                                         # an /amcl_pose close to what was sent (an older one does not count)
        return [m for m in confirmed if math.hypot(m.pose.pose.position.x - x, m.pose.pose.position.y - y) <= 0.5]
    answer_by = time.time() + 10.0
    while not taken() and time.time() < answer_by:
        time.sleep(0.2)
    if not taken():
        print("set_initial_pose: AMCL did not publish %s near this pose within 10 s" % args.confirm_topic, file=sys.stderr)
        return 1
    got = taken()[-1].pose.pose.position
    print("set_initial_pose: AMCL took it: x %.2f  y %.2f" % (got.x, got.y))
    return 0


if __name__ == "__main__":
    sys.exit(main())
