#!/usr/bin/env python3
# Draws a noise map from noise_log.csv (made by noise_logger.py).
# Louder spots = darker, bigger dots. Quiet spots = light, small dots.
#
# Run:  python3 plot_noise_map.py
#       python3 plot_noise_map.py run2.csv      (use a different log file)
# Output: noise_map.png

import sys
import csv
import matplotlib
matplotlib.use('Agg')   # save to file, no screen needed (works over SSH)
import matplotlib.pyplot as plt

in_file = sys.argv[1] if len(sys.argv) > 1 else 'noise_log.csv'
out_file = in_file.rsplit('.', 1)[0].replace('noise_log', 'noise_map') + '.png'
if out_file == in_file or not out_file.endswith('.png'):
    out_file = 'noise_map.png'

xs, ys, dbs = [], [], []
with open(in_file) as f:
    for row in csv.DictReader(f):
        xs.append(float(row['x_m']))
        ys.append(float(row['y_m']))
        dbs.append(float(row['noise_dB']))

if not dbs:
    sys.exit('No data in ' + in_file)

lo, hi = min(dbs), max(dbs)
span = (hi - lo) or 1.0
# Bigger dots for louder readings
sizes = [20 + 180 * (d - lo) / span for d in dbs]

fig, ax = plt.subplots(figsize=(8, 8))

# Robot's path as a thin grey line
ax.plot(xs, ys, color='lightgrey', linewidth=1, zorder=1)

# Noise dots: 'Greys' colour scale -> quiet = white/light, loud = black
dots = ax.scatter(xs, ys, c=dbs, cmap='Greys', vmin=lo, vmax=hi,
                  s=sizes, edgecolors='grey', linewidths=0.4, zorder=2)

# Start and end markers
ax.plot(xs[0], ys[0], marker='^', color='green', markersize=12,
        label='Start', zorder=3, linestyle='None')
ax.plot(xs[-1], ys[-1], marker='s', color='red', markersize=10,
        label='End', zorder=3, linestyle='None')

# Mark the loudest spot
i = dbs.index(hi)
ax.annotate('Loudest: %.1f dB' % hi, (xs[i], ys[i]),
            xytext=(10, 10), textcoords='offset points', fontsize=9)

cbar = fig.colorbar(dots, ax=ax, shrink=0.8)
cbar.set_label('Noise level (dB, closer to 0 = louder)')

ax.set_title('TurtleBot Noise Map  (%d readings)' % len(dbs))
ax.set_xlabel('x (m)')
ax.set_ylabel('y (m)')
ax.set_aspect('equal', adjustable='datalim')
ax.grid(True, alpha=0.3)
ax.legend(loc='best')

plt.tight_layout()
plt.savefig(out_file, dpi=150)
print('Saved', out_file)
print('Quietest: %.1f dB   Loudest: %.1f dB   Average: %.1f dB'
      % (lo, hi, sum(dbs) / len(dbs)))
