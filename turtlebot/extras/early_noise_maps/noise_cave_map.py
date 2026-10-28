#!/usr/bin/env python3
# Noise "cave" map: a fixed 10 m x 2 m map that starts fully unexplored
# (blank white). As the robot drives, the area around it is filled in
# and shaded by how loud it was there:  light grey = quiet, black = loud.
#
# The map is published as an image on /noise_cave_map, so you can watch it
# live in your browser through web_video_server. When you press Ctrl-C it
# also saves the final map as noise_cave_map.png.
#
# Needs noise_logger.py running at the same time (it publishes /noise_level
# and /noise_balance). The title shows which side the sound is louder on.
#
# Put the robot at one short end of the area, in the middle, facing along
# the 10 m side, then start THIS script. Wherever the robot is and whichever
# way it faces when the script starts becomes x=0, y=0, facing +x (right).
# No need to restart minimal.launch - just restart this script to reset.
#
# Run:  python3 noise_cave_map.py
# Options:  _length:=10  _width:=2   (map size in metres)
#           _x_start:=0             (where the map starts, relative to robot start)
#           _save:=run1_map.png     (file saved at the end)
# View: http://localhost:8080/stream_viewer?topic=/noise_cave_map

import math
import threading
import numpy as np
import matplotlib
matplotlib.use('Agg')   # draw to memory, no screen needed
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

CELL = 0.1      # grid cell size in metres (10 cm squares)
RADIUS = 0.3    # how far around the robot each reading carves open (metres)
BLANK = 'white'   # colour of unexplored area
# Grey scale that starts at light grey (not white) so quiet spots stand out
# from the blank unexplored area
from matplotlib.colors import LinearSegmentedColormap
NOISE_CMAP = LinearSegmentedColormap.from_list(
    'noise_greys', plt.get_cmap('Greys')(np.linspace(0.18, 1.0, 256)))


class NoiseGrid:
    def __init__(self, length=10.0, width=2.0, x_start=0.0):
        self.length, self.width = length, width
        self.x0, self.x1 = x_start, x_start + length
        self.y0, self.y1 = -width / 2.0, width / 2.0
        self.nx = int(round(length / CELL))
        self.ny = int(round(width / CELL))
        self.sum = np.zeros((self.ny, self.nx))
        self.cnt = np.zeros((self.ny, self.nx))
        xs = self.x0 + (np.arange(self.nx) + 0.5) * CELL
        ys = self.y0 + (np.arange(self.ny) + 0.5) * CELL
        self.cx, self.cy = np.meshgrid(xs, ys)
        self.loudest = None   # (x, y, dB)
        self.last_db = None

    def add(self, x, y, db):
        self.last_db = db
        near = (self.cx - x) ** 2 + (self.cy - y) ** 2 <= RADIUS ** 2
        if not near.any():
            return False      # robot is outside the map area
        self.sum[near] += db
        self.cnt[near] += 1
        if self.loudest is None or db > self.loudest[2]:
            self.loudest = (x, y, db)
        return True

    def mean(self):
        with np.errstate(invalid='ignore', divide='ignore'):
            m = self.sum / self.cnt
        return np.ma.masked_where(self.cnt == 0, m)

    def explored_pct(self):
        return 100.0 * (self.cnt > 0).mean()

    def inside(self, x, y):
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def render(fig, grid, cur, yaw, balance=None):
    """Draw the map and return (width, height, RGB bytes)."""
    fig.clf()
    ax = fig.add_subplot(111)
    ax.set_facecolor(BLANK)
    ax.grid(True, color='#dddddd', linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)

    m = grid.mean()
    if m.count() > 0:
        lo, hi = float(m.min()), float(m.max())
        if hi - lo < 1.0:
            hi = lo + 1.0
        im = ax.imshow(m, origin='lower', cmap=NOISE_CMAP, vmin=lo, vmax=hi,
                       extent=[grid.x0, grid.x1, grid.y0, grid.y1],
                       interpolation='nearest', zorder=1)
        cbar = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02)
        cbar.set_label('dB (louder ->)', fontsize=8)
        cbar.ax.tick_params(labelsize=7)

    if grid.loudest is not None:
        ax.plot(grid.loudest[0], grid.loudest[1], marker='*', color='orange',
                markersize=14, markeredgecolor='black', linestyle='None', zorder=3)

    # Robot position + heading arrow
    x, y = cur
    ax.annotate('', xy=(x + 0.35 * math.cos(yaw), y + 0.35 * math.sin(yaw)),
                xytext=(x, y), zorder=4,
                arrowprops=dict(arrowstyle='->', color='red', lw=2))
    ax.plot(x, y, marker='o', color='red', markersize=9, linestyle='None', zorder=5)

    # Which side is louder (shown as text in the title)
    side = None
    if balance is not None and abs(balance) >= 1.5:
        side = 'LEFT' if balance > 0 else 'RIGHT'

    ax.set_xlim(grid.x0, grid.x1)
    ax.set_ylim(grid.y0, grid.y1)
    ax.set_aspect('equal')
    ax.set_xticks(np.arange(grid.x0, grid.x1 + 0.001, 1.0))
    ax.set_yticks(np.arange(grid.y0, grid.y1 + 0.001, 0.5))
    ax.tick_params(labelsize=8)
    ax.set_xlabel('x (m)', fontsize=9)
    ax.set_ylabel('y (m)', fontsize=9)

    title = 'Noise Cave Map  %g x %g m   |   explored %d%%' % (
        grid.length, grid.width, grid.explored_pct())
    if grid.last_db is not None:
        title += '   |   now %.1f dB' % grid.last_db
    if grid.loudest is not None:
        title += '   |   loudest %.1f dB' % grid.loudest[2]
    if balance is not None:
        if side:
            title += '   |   sound from %s (%+.1f dB)' % (side, balance)
        else:
            title += '   |   sound from centre'
    if not grid.inside(x, y):
        title += '   |   ROBOT OUTSIDE MAP'
    ax.set_title(title, fontsize=10)

    handles = [Patch(facecolor=BLANK, edgecolor='grey', label='Unexplored'),
               Patch(color=NOISE_CMAP(0.0), label='Quiet'),
               Patch(color='black', label='Loud'),
               Line2D([], [], marker='o', color='red', linestyle='None', label='Robot'),
               Line2D([], [], marker='*', color='orange', markeredgecolor='black',
                      markersize=12, linestyle='None', label='Loudest spot')]
    ax.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, -0.32),
              ncol=5, fontsize=8, frameon=False)

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

    rospy.init_node('noise_cave_map')
    length = float(rospy.get_param('~length', 10.0))
    width = float(rospy.get_param('~width', 2.0))
    x_start = float(rospy.get_param('~x_start', 0.0))
    save_file = rospy.get_param('~save', 'noise_cave_map.png')

    grid = NoiseGrid(length, width, x_start)
    lock = threading.Lock()
    state = {'pos': [0.0, 0.0], 'yaw': 0.0, 'origin': None, 'balance': None}

    def odom_cb(msg):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y),
                         1 - 2 * (q.y * q.y + q.z * q.z))
        # First reading = map origin: robot's current spot is (0, 0) facing +x
        if state['origin'] is None:
            state['origin'] = (p.x, p.y, yaw)
            rospy.loginfo('Map origin set at robot position. Drive towards +x.')
        ox, oy, oyaw = state['origin']
        dx, dy = p.x - ox, p.y - oy
        c, s = math.cos(-oyaw), math.sin(-oyaw)
        state['pos'] = [c * dx - s * dy, s * dx + c * dy]
        state['yaw'] = math.atan2(math.sin(yaw - oyaw), math.cos(yaw - oyaw))

    def noise_cb(msg):
        x, y = state['pos']
        with lock:
            grid.add(x, y, msg.data)

    rospy.Subscriber('/odom', Odometry, odom_cb)
    rospy.Subscriber('/noise_level', Float32, noise_cb)

    def balance_cb(msg):
        state['balance'] = msg.data
    rospy.Subscriber('/noise_balance', Float32, balance_cb)
    pub = rospy.Publisher('/noise_cave_map', Image, queue_size=1)

    fig = plt.figure(figsize=(12, 3.6), dpi=100)
    rate = rospy.Rate(1)    # redraw once per second
    rospy.loginfo('Publishing live map on /noise_cave_map  (Ctrl-C to stop and save)')

    while not rospy.is_shutdown():
        with lock:
            w, h, data = render(fig, grid, list(state['pos']), state['yaw'], state['balance'])
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

    # Save the final map
    with lock:
        render(fig, grid, list(state['pos']), state['yaw'], state['balance'])
    fig.savefig(save_file, dpi=150)
    print('Saved final map to %s  (explored %d%%)' % (save_file, grid.explored_pct()))


if __name__ == '__main__':
    main()
