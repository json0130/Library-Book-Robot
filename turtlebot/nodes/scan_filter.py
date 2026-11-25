#!/usr/bin/env python3
# Removes LiDAR points that hit the robot itself (the TurtleBot's posts, cables,
# camera...). Anything closer than min_range is ignored.
#
# Reads /hokuyo_scan  ->  publishes /hokuyo_scan_filtered
# Navigation (AMCL + move_base) uses the filtered scan.
#
# Run:  python3 scan_filter.py
# Option: _min_range:=0.3   (ignore everything closer than 30 cm)

import rospy
from sensor_msgs.msg import LaserScan


def main():
    rospy.init_node('scan_filter')
    min_r = float(rospy.get_param('~min_range', 0.25))
    pub = rospy.Publisher('/hokuyo_scan_filtered', LaserScan, queue_size=5)

    def cb(msg):
        # Raising range_min makes AMCL and the costmaps skip close points
        msg.range_min = max(msg.range_min, min_r)
        msg.ranges = [r if r >= min_r else float('inf') for r in msg.ranges]
        pub.publish(msg)

    rospy.Subscriber('/hokuyo_scan', LaserScan, cb, queue_size=5)
    rospy.loginfo('Scan filter: ignoring LiDAR points closer than %.2f m', min_r)
    rospy.spin()


if __name__ == '__main__':
    main()
