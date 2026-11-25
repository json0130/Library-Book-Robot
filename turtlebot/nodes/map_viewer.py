#!/usr/bin/env python3
# Shows the map live in your browser (through web_video_server), with distances.
#   Grey = unknown, white = free space, black = walls/obstacles, red = robot.
#   - 1 m grid with metre labels
#   - rings around the robot at 1, 2, 3 m
#   - distance to the first obstacle in front / left / right of the robot
#   - nearest obstacle in any direction (orange)
#   - robot position, distance from the start, total distance driven
#
# Run:  python3 map_viewer.py
# View: http://localhost:8080/stream_viewer?topic=/map_image

import math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

UNKNOWN = (205, 205, 205)
FREE = (255, 255, 255)
WALL = (30, 30, 30)
RAY_MAX = 6.0           # look this far for obstacles (m)
NEAR_MAX = 5.0          # nearest-obstacle search radius (m)


def grid_to_rgb(g):
    img = np.empty(g.shape + (3,), dtype=np.uint8)
    img[:] = UNKNOWN
    img[(g >= 0) & (g < 50)] = FREE
    img[g >= 50] = WALL
    return img


def cast_ray(g, res, ox, oy, x, y, ang, max_d=RAY_MAX):
    """Walk from (x, y) in direction ang. Returns (distance, 'wall'|'unknown'|'clear')."""
    h, w = g.shape
    step = res / 2.0
    d = 0.15                                   # start just outside the robot
    while d <= max_d:
        px, py = x + d * math.cos(ang), y + d * math.sin(ang)
        c, r = int((px - ox) / res), int((py - oy) / res)
        if not (0 <= r < h and 0 <= c < w):
            return d, 'unknown'
        v = g[r, c]
        if v >= 50:
            return d, 'wall'
        if v < 0:
            return d, 'unknown'
        d += step
    return max_d, 'clear'


def nearest_obstacle(g, res, ox, oy, x, y, max_d=NEAR_MAX):
    """Closest occupied cell to (x, y). Returns (distance, ox, oy) or None."""
    h, w = g.shape
    n = int(max_d / res)
    rc, cc = int((y - oy) / res), int((x - ox) / res)
    r0, r1 = max(0, rc - n), min(h, rc + n + 1)
    c0, c1 = max(0, cc - n), min(w, cc + n + 1)
    occ = np.argwhere(g[r0:r1, c0:c1] >= 50)
    if not len(occ):
        return None
    wx = ox + (occ[:, 1] + c0 + 0.5) * res
    wy = oy + (occ[:, 0] + r0 + 0.5) * res
    d = np.hypot(wx - x, wy - y)
    i = int(np.argmin(d))
    if d[i] > max_d:
        return None
    return float(d[i]), float(wx[i]), float(wy[i])


def render_map(fig, data, w, h, res, ox, oy, robot=None, start=None, travelled=0.0,
               margin=1.0):
    """Draw the annotated map into fig and return an RGB numpy array."""
    g = np.asarray(data, dtype=np.int16).reshape(h, w)
    fig.clf()
    ax = fig.add_axes([0.07, 0.06, 0.9, 0.86])

    # Area to show: explored part + robot, plus a margin
    known = np.argwhere(g >= 0)
    if len(known):
        (r0, c0), (r1, c1) = known.min(axis=0), known.max(axis=0)
        xmin, xmax = ox + c0 * res, ox + (c1 + 1) * res
        ymin, ymax = oy + r0 * res, oy + (r1 + 1) * res
    else:
        xmin = xmax = robot[0] if robot else 0.0
        ymin = ymax = robot[1] if robot else 0.0
    if robot:
        xmin, xmax = min(xmin, robot[0]), max(xmax, robot[0])
        ymin, ymax = min(ymin, robot[1]), max(ymax, robot[1])
    xmin, xmax, ymin, ymax = xmin - margin, xmax + margin, ymin - margin, ymax + margin
    # at least 4 m across so it doesn't zoom in too far
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    half = max(2.0, (xmax - xmin) / 2, (ymax - ymin) / 2)
    xmin, xmax, ymin, ymax = cx - half, cx + half, cy - half, cy + half

    ax.imshow(grid_to_rgb(g), origin='lower', interpolation='nearest',
              extent=[ox, ox + w * res, oy, oy + h * res], zorder=0)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect('equal')

    # 1 m grid
    span = xmax - xmin
    step = 1.0 if span < 25 else 2.0
    ax.set_xticks(np.arange(math.ceil(xmin), xmax, step))
    ax.set_yticks(np.arange(math.ceil(ymin), ymax, step))
    ax.grid(True, color='#4a90d9', alpha=0.25, linewidth=0.6)
    ax.tick_params(labelsize=8)
    ax.set_xlabel('x (m)', fontsize=9)
    ax.set_ylabel('y (m)', fontsize=9)

    info = []
    if start:
        ax.plot(start[0], start[1], marker='^', color='green', markersize=10, zorder=5)
        ax.annotate('start', (start[0], start[1]), xytext=(6, -12),
                    textcoords='offset points', fontsize=8, color='green')

    if robot:
        x, y, yaw = robot
        # distance rings
        for r in (1, 2, 3):
            ax.add_patch(Circle((x, y), r, fill=False, color='#d62728', alpha=0.35,
                                linestyle='--', linewidth=0.8, zorder=2))
            ax.text(x + r * 0.707, y + r * 0.707, '%d m' % r, fontsize=7,
                    color='#d62728', alpha=0.8, zorder=2)

        # rays: front / left / right
        for name, off, col in (('front', 0.0, '#1f6feb'),
                               ('left', math.pi / 2, '#7b3fe4'),
                               ('right', -math.pi / 2, '#7b3fe4')):
            d, kind = cast_ray(g, res, ox, oy, x, y, yaw + off)
            ex, ey = x + d * math.cos(yaw + off), y + d * math.sin(yaw + off)
            ax.plot([x, ex], [y, ey], color=col, linewidth=1.6,
                    linestyle='-' if kind == 'wall' else ':', zorder=3)
            if kind == 'wall':
                ax.plot(ex, ey, marker='x', color=col, markersize=7, zorder=4)
                label = '%.2f m' % d
            elif kind == 'clear':
                label = '> %.0f m' % d
            else:
                label = '%.1f m?' % d
            ax.annotate(label, ((x + ex) / 2, (y + ey) / 2), fontsize=9, color=col,
                        fontweight='bold', ha='center', zorder=6,
                        bbox=dict(boxstyle='round,pad=0.15', fc='white', ec=col, alpha=0.85))
            info.append('%-5s %s' % (name, label))

        # nearest obstacle in any direction
        near = nearest_obstacle(g, res, ox, oy, x, y)
        if near:
            d, nx, ny = near
            ax.plot([x, nx], [y, ny], color='#ff7f0e', linewidth=2, zorder=3)
            ax.plot(nx, ny, marker='o', color='#ff7f0e', markersize=5, zorder=4)
            info.insert(0, 'nearest %.2f m' % d)

        # robot
        ax.add_patch(Circle((x, y), 0.17, color='#e02020', zorder=7))
        ax.plot([x, x + 0.35 * math.cos(yaw)], [y, y + 0.35 * math.sin(yaw)],
                color='#e02020', linewidth=3, zorder=7)

        info.insert(0, 'robot x %.2f  y %.2f' % (x, y))
        if start:
            info.insert(1, 'from start %.2f m' % math.hypot(x - start[0], y - start[1]))
        info.insert(2 if start else 1, 'driven %.2f m' % travelled)

    ax.set_title('Live map' + ('' if robot else '  (waiting for robot position)'), fontsize=11)
    if info:
        fig.text(0.985, 0.985, '\n'.join(info), ha='right', va='top', family='monospace',
                 fontsize=9, bbox=dict(boxstyle='round', fc='white', ec='#999999', alpha=0.9))

    fig.canvas.draw()
    W, H = fig.canvas.get_width_height()
    return np.asarray(fig.canvas.buffer_rgba()).reshape(H, W, 4)[:, :, :3].copy()


def main():
    import rospy
    import tf2_ros
    from nav_msgs.msg import OccupancyGrid
    from sensor_msgs.msg import Image

    rospy.init_node('map_viewer')
    base = rospy.get_param('~base_frame', 'base_footprint')
    latest = {'map': None}

    def map_cb(msg):
        latest['map'] = msg

    rospy.Subscriber('/map', OccupancyGrid, map_cb)
    pub = rospy.Publisher('/map_image', Image, queue_size=1)
    tf_buf = tf2_ros.Buffer()
    tf2_ros.TransformListener(tf_buf)

    fig = plt.figure(figsize=(8, 8), dpi=100)
    start, last, travelled = None, None, 0.0
    rospy.loginfo('Publishing live map on /map_image  (Ctrl-C to stop)')
    rate = rospy.Rate(2)
    warned = False
    while not rospy.is_shutdown():
        m = latest['map']
        if m is None:
            if not warned:
                rospy.loginfo('Waiting for /map ...')
                warned = True
        else:
            robot = None
            try:
                t = tf_buf.lookup_transform('map', base, rospy.Time(0))
                q = t.transform.rotation
                yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
                robot = (t.transform.translation.x, t.transform.translation.y, yaw)
                if start is None:
                    start = robot[:2]
                if last is not None:
                    step = math.hypot(robot[0] - last[0], robot[1] - last[1])
                    if step < 1.0:            # ignore jumps when the map corrects itself
                        travelled += step
                last = robot[:2]
            except Exception:
                pass
            i = m.info
            try:
                img = render_map(fig, m.data, i.width, i.height, i.resolution,
                                 i.origin.position.x, i.origin.position.y,
                                 robot, start, travelled)
            except Exception as e:             # keep running even if one frame fails
                rospy.logerr_throttle(10, 'Map drawing failed: %r' % e)
                rate.sleep()
                continue
            msg = Image()
            msg.header.stamp = rospy.Time.now()
            msg.height, msg.width = img.shape[0], img.shape[1]
            msg.encoding = 'rgb8'
            msg.step = img.shape[1] * 3
            msg.data = img.tobytes()
            pub.publish(msg)
        try:
            rate.sleep()
        except rospy.ROSInterruptException:
            break


if __name__ == '__main__':
    main()
