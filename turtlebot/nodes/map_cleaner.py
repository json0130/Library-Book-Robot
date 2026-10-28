#!/usr/bin/env python3
# Cleans the camera-made map for path planning.
# The camera map has lots of small black specks (noisy depth near chair legs,
# shiny or dark surfaces). The planner treats every speck as a wall and can't
# find a way through. This removes small, isolated black blobs; real walls
# and furniture (big connected blobs) stay. Real small obstacles are still
# avoided, because the LiDAR sees them live while driving.
#
# Reads /map  ->  publishes /map_clean   (used by the planner and the web page)
#
# Run:  python3 map_cleaner.py
# Options:  _min_area:=0.05   remove black blobs smaller than this (m^2)
#                             (0.05 m^2 = e.g. 22 x 22 cm)

from collections import deque

import numpy as np

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


def main():
    import rospy
    from nav_msgs.msg import OccupancyGrid

    rospy.init_node('map_cleaner')
    min_area = float(rospy.get_param('~min_area', 0.05))
    pub = rospy.Publisher('/map_clean', OccupancyGrid, queue_size=1, latch=True)

    def cb(msg):
        i = msg.info
        g = np.asarray(msg.data, dtype=np.int16).reshape(i.height, i.width)
        clean, removed = clean_grid(g, i.resolution, min_area)
        out = OccupancyGrid()
        out.header = msg.header
        out.info = msg.info
        out.data = clean.astype(np.int8).ravel().tolist()
        pub.publish(out)
        rospy.loginfo('Map cleaned: removed %d small black specks (< %.2f m^2)', removed, min_area)

    rospy.Subscriber('/map', OccupancyGrid, cb, queue_size=1)
    rospy.loginfo('Map cleaner: /map -> /map_clean')
    rospy.spin()


if __name__ == '__main__':
    main()
