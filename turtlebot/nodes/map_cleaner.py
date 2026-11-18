#!/usr/bin/env python3
# Cleans the map for path planning, and paints the keep-out zones in.
# The camera map has lots of small black specks (noisy depth near chair legs,
# shiny or dark surfaces). The planner treats every speck as a wall and can't
# find a way through. This removes small, isolated black blobs; real walls
# and furniture (big connected blobs) stay. Real small obstacles are still
# avoided, because the LiDAR sees them live while driving.
# Keep-out zones (config/keepout.yaml, keepout.py: under the desks, say) are
# painted in as walls AFTER the cleaning, so a small zone is never removed as
# a speck. Only the planner's map gets them: AMCL reads /map, which stays as it is.
#
# Reads /map  ->  publishes /map_clean   (used by the planner and the web page)
#
# Run:  python3 map_cleaner.py
# Options:  _min_area:=0.05   remove black blobs smaller than this (m^2)
#                             (0.05 m^2 = e.g. 22 x 22 cm)
#           _keepout:=/path/config/keepout.yaml     paint these zones in (run_robot_link.sh --amcl passes it: the zones are
#                                                    LiDAR-map coordinates, so never with RTAB-Map)
#           _floorplan:=/path/data/floorplan.yaml   _shelves:=/path/data/shelves.csv
#                             the station and shelf poses no zone may cover (keepout.check)
# If the zones cannot be used (a zone off the map, over the station...), NO
# /map_clean is published: the planner then has no map and the robot does not
# drive. run_robot_link.sh checks the same thing before it starts anything.

import os
import sys
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keepout                                      # noqa: E402

OCC = 50    # cells >= 50 are obstacles


def label_blobs(occ):
    """8-connected blob labels for a boolean grid. Returns (labels, count)."""
    try:
        from scipy import ndimage
        return ndimage.label(occ, structure=np.ones((3, 3), int))
    except ImportError:
        pass
    h, w = occ.shape
    labels = np.zeros((h, w), np.int32)
    n = 0
    for r0, c0 in np.argwhere(occ):
        if labels[r0, c0]:
            continue
        n += 1
        labels[r0, c0] = n
        q = deque([(r0, c0)])
        while q:
            r, c = q.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < h and 0 <= cc < w and occ[rr, cc] and not labels[rr, cc]:
                        labels[rr, cc] = n
                        q.append((rr, cc))
    return labels, n


def clean_grid(g, res, min_area=0.05):
    """Remove black blobs smaller than min_area (m^2). Returns (clean grid, blobs removed)."""
    g = np.asarray(g, dtype=np.int16)
    occ = g >= OCC
    if not occ.any():
        return g.copy(), 0
    labels, n = label_blobs(occ)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    min_cells = max(1, int(round(min_area / (res * res))))
    small = sizes < min_cells
    small[0] = False                     # label 0 = not an obstacle
    remove = small[labels]
    out = g.copy()
    out[remove] = 0                      # tiny specks -> free space
    return out, int(small.sum())


def planner_grid(g, res, origin_x, origin_y, min_area=0.05, zones=()):
    """The map the planner gets: cleaned, then the keep-out zones painted in. Returns (grid, specks removed, {zone: cells})."""
    clean, removed = clean_grid(g, res, min_area)
    painted, counts = keepout.paint(clean, res, origin_x, origin_y, zones)
    return painted, removed, counts


def grid_bounds(info):
    """(x0, y0, x1, y1) of an OccupancyGrid's info, in metres."""
    x0, y0 = info.origin.position.x, info.origin.position.y
    return x0, y0, x0 + info.width * info.resolution, y0 + info.height * info.resolution


def main():
    import rospy
    from nav_msgs.msg import OccupancyGrid

    rospy.init_node('map_cleaner')
    min_area = float(rospy.get_param('~min_area', 0.05))
    zone_file = rospy.get_param('~keepout', '')
    floorplan = rospy.get_param('~floorplan', '')
    shelves = rospy.get_param('~shelves', '')
    pub = rospy.Publisher('/map_clean', OccupancyGrid, queue_size=1, latch=True)
    zones, clearance, places, refused = [], 0.0, {}, []
    if zone_file:
        try:
            zones, clearance = keepout.load(zone_file)
            places = keepout.poses(floorplan or None, shelves or None) if zones else {}
        except keepout.KeepoutError as exc:
            refused = exc.problems
        else:
            rospy.loginfo('Keep-out zones from %s: %s', zone_file, ', '.join(z.name for z in zones) or 'none')

    def cb(msg):
        i = msg.info
        o = i.origin.orientation
        problems = list(refused)
        if zones and abs(o.z) > 1e-6:
            problems.append('the map is rotated: keep-out zones need a map with yaw 0')
        if zones:
            problems += keepout.check(zones, grid_bounds(i), places, clearance)
        if problems:
            for line in problems:
                rospy.logerr('Keep-out zones (%s): %s', zone_file, line)
            rospy.logerr('NO /map_clean is published, so the planner has no map and the robot will not drive. Fix %s '
                         '(python3 tools/keepout_preview.py shows the zones on the map) and start again.', zone_file)
            return
        g = np.asarray(msg.data, dtype=np.int16).reshape(i.height, i.width)
        clean, removed, painted = planner_grid(g, i.resolution, i.origin.position.x, i.origin.position.y, min_area, zones)
        out = OccupancyGrid()
        out.header = msg.header
        out.info = msg.info
        out.data = clean.astype(np.int8).ravel().tolist()
        pub.publish(out)
        rospy.loginfo('Map cleaned: removed %d small black specks (< %.2f m^2)%s', removed, min_area,
                      ''.join('; keep-out %s: %d cells' % kv for kv in sorted(painted.items())))

    rospy.Subscriber('/map', OccupancyGrid, cb, queue_size=1)
    rospy.loginfo('Map cleaner: /map -> /map_clean')
    rospy.spin()


if __name__ == '__main__':
    main()
