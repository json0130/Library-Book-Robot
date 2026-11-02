#!/usr/bin/env python3
# How often does RTAB-Map publish /rtabmap/localization_pose, standing still and while moving?
# The answer sets robot_link_api's `_loc_max_age_s` (how old the last localisation may be before a MOVING robot counts as lost).
#
# Needs the stack running (run_robot_link.sh, or run_vslam_noise_nav.sh). It only listens: it sends nothing and moves nothing.
# Stand the robot still for the first half of the time; then move it (push it by hand, or drive it with the click-to-go page /
# keyboard teleop - someone at the power switch) for the second half.
#
# Run:  python3 measure_localisation.py            (60 s)
#       python3 measure_localisation.py --seconds 120

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarise(events, moving_mps=0.02, turning_rps=0.05):
    """events: [(time, 'loc' | 'odom', speed_mps, turn_rps)]. Returns {'still': {...}, 'moving': {...}} of the gaps between localisations,
    each gap counted as 'moving' if the robot was moving when the later localisation arrived."""
    out = {"still": [], "moving": []}
    moving, last = False, None
    for t, kind, v, w in events:
        if kind == "odom":
            moving = abs(v) > moving_mps or abs(w) > turning_rps
        else:
            if last is not None:
                out["moving" if moving else "still"].append(t - last)
            last = t
    result = {}
    for key, gaps in out.items():
        result[key] = {"n": len(gaps), "median": percentile(gaps, 0.5), "p95": percentile(gaps, 0.95), "max": max(gaps) if gaps else None}
    return result


def suggest_loc_max_age(stats, floor_s=3.0):
    """Twice the 95th percentile gap while moving (so one missed update is not 'lost'), at least floor_s; None without data."""
    p95 = stats["moving"]["p95"]
    return None if p95 is None else round(max(floor_s, 2 * p95), 1)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--seconds", type=float, default=60.0)
    args = p.parse_args(argv)
    import rospy
    from geometry_msgs.msg import PoseWithCovarianceStamped
    from nav_msgs.msg import Odometry
    rospy.init_node("measure_localisation", anonymous=True, disable_signals=True)
    events = []
    rospy.Subscriber("/odom", Odometry, lambda m: events.append((time.time(), "odom", m.twist.twist.linear.x, m.twist.twist.angular.z)), queue_size=5)
    rospy.Subscriber("/rtabmap/localization_pose", PoseWithCovarianceStamped, lambda m: events.append((time.time(), "loc", 0.0, 0.0)), queue_size=5)
    print("Listening for %g s. Keep the robot still for the first half, then move it." % args.seconds)
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            time.sleep(1.0)
            print("\r  %3.0f s left, %d localisations so far " % (end - time.time(), sum(1 for e in events if e[1] == "loc")), end="", flush=True)
    except KeyboardInterrupt:
        pass
    print()
    stats = summarise(events)
    for key in ("still", "moving"):
        s = stats[key]
        if s["n"]:
            print("  %-7s %3d gaps: median %.2f s, 95%% under %.2f s, longest %.2f s" % (key, s["n"], s["median"], s["p95"], s["max"]))
        else:
            print("  %-7s no localisations (RTAB-Map said nothing while the robot was %s)" % (key, key))
    suggestion = suggest_loc_max_age(stats)
    if suggestion is None:
        print("Not enough data for a suggestion: move the robot while this runs.")
    else:
        print("Suggested  _loc_max_age_s:=%g" % suggestion)
    return 0


if __name__ == "__main__":
    sys.exit(main())
