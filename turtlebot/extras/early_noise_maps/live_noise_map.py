#!/usr/bin/env python3
# Live noise map: redraws the map every second while you drive and
# publishes it as an image topic (/noise_map), so you can watch it in
# your browser through web_video_server - the same way as the camera.
#
# Needs noise_logger.py running at the same time (it publishes /noise_level).
#
# Run:  python3 live_noise_map.py
# View: http://localhost:8080/stream_viewer?topic=/noise_map

import threading
import numpy as np
import matplotlib
matplotlib.use('Agg')   # draw to memory, no screen needed
import matplotlib.pyplot as plt

points = []             # list of (x, y, dB)
pos = [0.0, 0.0]
lock = threading.Lock()


def render(fig, pts, cur):
    """Draw the map and return (width, height, RGB bytes)."""
    fig.clf()
    ax = fig.add_subplot(111)

    if pts:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        dbs = [p[2] for p in pts]
        lo, hi = min(dbs), max(dbs)
        span = (hi - lo) or 1.0
        sizes = [20 + 180 * (d - lo) / span for d in dbs]

        ax.plot(xs, ys, color='lightgrey', linewidth=1, zorder=1)
        dots = ax.scatter(xs, ys, c=dbs, cmap='Greys', vmin=lo, vmax=hi,
                          s=sizes, edgecolors='grey', linewidths=0.4, zorder=2)
        cbar = fig.colorbar(dots, ax=ax, shrink=0.8)
        cbar.set_label('Noise (dB, closer to 0 = louder)')
        ax.plot(xs[0], ys[0], marker='^', color='green', markersize=11,
                linestyle='None', label='Start', zorder=3)
        title = 'Live Noise Map  |  now: %.1f dB  |  loudest: %.1f dB' % (dbs[-1], hi)
    else:
        title = 'Live Noise Map  |  waiting for /noise_level ...'

    # Robot's current position
    ax.plot(cur[0], cur[1], marker='o', color='red', markersize=10,
            linestyle='None', label='Robot', zorder=4)

    # Keep at least a 2 m x 2 m view so it doesn't zoom in too far
    allx = [p[0] for p in pts] + [cur[0]]
    ally = [p[1] for p in pts] + [cur[1]]
    cx, cy = (min(allx) + max(allx)) / 2, (min(ally) + max(ally)) / 2
    half = max(1.0, (max(allx) - min(allx)) / 2 + 0.3,
               (max(ally) - min(ally)) / 2 + 0.3)
    ax.set_xlim(cx - half, cx + half)
    ax.set_ylim(cy - half, cy + half)
    ax.set_aspect('equal')

    ax.set_title(title, fontsize=10)
    ax.set_xlabel('x (m)')
    ax.set_ylabel('y (m)')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left', fontsize=8)
    fig.tight_layout()

    fig.canvas.draw()
    w, h = fig.canvas.get_width_height()
    rgba = np.asarray(fig.canvas.buffer_rgba()).reshape(h, w, 4)
    return w, h, rgba[:, :, :3].tobytes()


def main():
    import rospy
    from sensor_msgs.msg import Image
    from std_msgs.msg import Float32
    from nav_msgs.msg import Odometry

    def odom_cb(msg):
        pos[0] = msg.pose.pose.position.x
        pos[1] = msg.pose.pose.position.y

    def noise_cb(msg):
        with lock:
            points.append((pos[0], pos[1], msg.data))

    rospy.init_node('live_noise_map')
    rospy.Subscriber('/odom', Odometry, odom_cb)
    rospy.Subscriber('/noise_level', Float32, noise_cb)
    pub = rospy.Publisher('/noise_map', Image, queue_size=1)

    fig = plt.figure(figsize=(6.4, 6.0), dpi=100)
    rate = rospy.Rate(1)    # redraw once per second
    rospy.loginfo('Publishing live map on /noise_map  (Ctrl-C to stop)')

    while not rospy.is_shutdown():
        with lock:
            pts = list(points)
        w, h, data = render(fig, pts, list(pos))

        img = Image()
        img.header.stamp = rospy.Time.now()
        img.height, img.width = h, w
        img.encoding = 'rgb8'
        img.step = w * 3
        img.data = data
        pub.publish(img)

        try:
            rate.sleep()
        except rospy.ROSInterruptException:
            break


if __name__ == '__main__':
    main()
