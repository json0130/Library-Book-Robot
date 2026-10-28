#!/usr/bin/env python3
# Click-to-go noise mapping.
#
# Opens a web page (port 8000) showing your saved LiDAR map. Click anywhere
# white on the map and the robot drives there by itself (move_base), while
# the noise it hears along the way is painted onto the map:
#     pale yellow = quiet  ->  orange  ->  dark red = loud
# Walls stay black, unknown areas grey, the robot is the blue dot.
#
# Needs running:  minimal.launch, config/vslam.launch (navigation), noise_logger.py
#                 (run_vslam_noise_nav.sh starts all of these)
#
# Run:  python3 noise_nav_web.py
# View: on your PC run  ssh -L 8000:localhost:8000 robot@<robot ip>
#       then open      http://localhost:8000
#
# When you press Ctrl-C (or the "Save" button) it saves:
#     noise_nav_map.png   the map with noise painted on it
#     noise_nav_log.csv   every reading: time, x, y, noise_dB

import io
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

RADIUS = 0.4            # metres around the robot each noise reading paints.
                        # The LEFT half of that circle gets the LEFT mic's level,
                        # the RIGHT half gets the RIGHT mic's level.
SIDE_DB = 1.5           # left/right difference needed to call it LEFT or RIGHT
UNKNOWN = np.array([205, 205, 205], np.uint8)
FREE = np.array([255, 255, 255], np.uint8)
WALL = np.array([25, 25, 25], np.uint8)
ROBOT = np.array([31, 111, 235], np.uint8)
PATH = np.array([90, 160, 255], np.uint8)
GOAL = np.array([22, 163, 74], np.uint8)
CMAP = plt.get_cmap('YlOrRd')


class NoiseNavCore:
    """Map + noise data + drawing. No ROS in here, so it can be tested alone."""

    def __init__(self, target_px=900):
        self.lock = threading.RLock()
        self.target_px = target_px
        self.grid = None
        self.robot = None          # (x, y, yaw)
        self.goal = None           # (x, y)
        self.path = []             # [(x, y), ...]
        self.readings = []         # [(t, x, y, yaw, dB, left_dB, right_dB), ...]
        self.last_db = None
        self.last_balance = None
        self.loudest = None        # (x, y, dB)
        self.nav_state = 'Waiting for a click'
        self.last_loc = None       # time the camera last recognised where we are
        self.view = 'loudest'      # 'loudest' = loudest heard at each spot, 'average' = typical level

    # ---------- map ----------
    def set_map(self, data, w, h, res, ox, oy, margin=10):
        with self.lock:
            g = np.asarray(data, dtype=np.int16).reshape(h, w)
            self.grid, self.w, self.h = g, w, h
            self.res, self.ox, self.oy = res, ox, oy
            self.sum = np.zeros((h, w), np.float64)
            self.cnt = np.zeros((h, w), np.int32)
            self.mx = np.full((h, w), -np.inf)
            n = int(math.ceil(RADIUS / res))
            yy, xx = np.mgrid[-n:n + 1, -n:n + 1]
            keep = (xx * res) ** 2 + (yy * res) ** 2 <= RADIUS ** 2
            self.disk = (yy[keep], xx[keep])
            for rd in self.readings:                    # re-paint old readings
                self._paint_reading(rd)
            known = np.argwhere(g >= 0)
            if len(known):
                (r0, c0), (r1, c1) = known.min(axis=0), known.max(axis=0)
            else:
                r0, c0, r1, c1 = 0, 0, h - 1, w - 1
            self.r0, self.c0 = max(0, r0 - margin), max(0, c0 - margin)
            self.r1, self.c1 = min(h - 1, r1 + margin), min(w - 1, c1 + margin)
            ch, cw = self.r1 - self.r0 + 1, self.c1 - self.c0 + 1
            self.scale = max(1, self.target_px // max(ch, cw))

    def has_map(self):
        return self.grid is not None

    def world_to_cell(self, x, y):
        return int((y - self.oy) / self.res), int((x - self.ox) / self.res)

    def cell_value(self, x, y):
        r, c = self.world_to_cell(x, y)
        if 0 <= r < self.h and 0 <= c < self.w:
            return int(self.grid[r, c])
        return -1

    def frac_to_world(self, fx, fy):
        """Click position as a fraction of the picture (0..1) -> map x, y (m)."""
        with self.lock:
            cw = self.c1 - self.c0 + 1
            ch = self.r1 - self.r0 + 1
            col = self.c0 + fx * cw
            row = self.r0 + (1.0 - fy) * ch       # picture is flipped vertically
            return float(self.ox + col * self.res), float(self.oy + row * self.res)

    # ---------- noise ----------
    def _paint(self, x, y, db, yaw=None, db_right=None):
        """Paint a circle at (x, y). If db_right is given, the left half
        (relative to the robot's heading yaw) gets db and the right half db_right."""
        r, c = self.world_to_cell(x, y)
        dy, dx = self.disk
        rr, cc = r + dy, c + dx
        ok = (rr >= 0) & (rr < self.h) & (cc >= 0) & (cc < self.w)
        rr, cc, dy, dx = rr[ok], cc[ok], dy[ok], dx[ok]
        free = self.grid[rr, cc] < 50                 # don't paint over walls
        rr, cc, dy, dx = rr[free], cc[free], dy[free], dx[free]
        vals = np.full(len(rr), float(db))
        if db_right is not None and yaw is not None:
            # which side of the robot's heading line each cell is on
            right_side = (math.cos(yaw) * dy - math.sin(yaw) * dx) < 0
            vals[right_side] = db_right
        np.add.at(self.sum, (rr, cc), vals)
        np.add.at(self.cnt, (rr, cc), 1)
        np.maximum.at(self.mx, (rr, cc), vals)

    def _paint_reading(self, rd):
        t, x, y, yaw, db, dl, dr = rd
        if dl is None or dr is None:
            self._paint(x, y, db)
        else:
            self._paint(x, y, dl, yaw=yaw, db_right=dr)

    @staticmethod
    def side_of(balance):
        if balance is None:
            return None
        return 'LEFT' if balance >= SIDE_DB else ('RIGHT' if balance <= -SIDE_DB else 'centre')

    def add_noise(self, db, t=None, left=None, right=None):
        """Add a reading. With left/right mic levels, the left half of the
        painted circle shows the left mic and the right half the right mic,
        so the louder side comes out darker."""
        with self.lock:
            self.last_db = db
            self.last_balance = None if left is None or right is None else left - right
            if self.robot is None or self.grid is None:
                return False
            x, y, yaw = self.robot
            rd = (t if t is not None else time.time(), x, y, yaw, db, left, right)
            self.readings.append(rd)
            peak = db if left is None else max(left, right)
            if self.loudest is None or peak > self.loudest[2]:
                self.loudest = (x, y, peak)
            self._paint_reading(rd)
            return True

    def cell_values(self, sl=(slice(None), slice(None))):
        """(mask of painted cells, their values) for the current view."""
        cnt = self.cnt[sl]
        has = cnt > 0
        if self.view == 'average':
            return has, self.sum[sl][has] / cnt[has]
        return has, self.mx[sl][has]

    def noise_range(self):
        if not self.readings:
            return None
        has, m = self.cell_values()
        if not has.any():
            return None
        lo, hi = float(m.min()), float(m.max())
        return lo, max(hi, lo + 1.0)

    # ---------- drawing ----------
    def render(self):
        with self.lock:
            r0, r1, c0, c1 = self.r0, self.r1, self.c0, self.c1
            g = self.grid[r0:r1 + 1, c0:c1 + 1]
            img = np.empty(g.shape + (3,), np.float32)
            img[:] = UNKNOWN
            img[(g >= 0) & (g < 50)] = FREE

            rng = self.noise_range()
            if rng:
                has, vals = self.cell_values((slice(r0, r1 + 1), slice(c0, c1 + 1)))
                norm = np.clip((vals - rng[0]) / (rng[1] - rng[0]), 0, 1)
                col = CMAP(0.15 + 0.85 * norm)[:, :3] * 255
                img[has] = 0.2 * img[has] + 0.8 * col
            img[g >= 50] = WALL
            img = img[::-1].astype(np.uint8)

            s = self.scale
            if s > 1:
                img = np.repeat(np.repeat(img, s, axis=0), s, axis=1)
            H = img.shape[0]

            def to_px(x, y):
                px = ((x - self.ox) / self.res - c0) * s
                py = H - ((y - self.oy) / self.res - r0) * s
                return px, py

            yy, xx = np.ogrid[:img.shape[0], :img.shape[1]]

            def dot(x, y, rad, colr, ring=False):
                px, py = to_px(x, y)
                d2 = (xx - px) ** 2 + (yy - py) ** 2
                mask = (d2 <= rad ** 2) & (d2 >= (rad - 3) ** 2) if ring else d2 <= rad ** 2
                img[mask] = colr

            for (x, y) in self.path[::3]:
                dot(x, y, max(2, s // 2), PATH)
            if self.goal:
                rad = max(7, int(0.15 / self.res * s))
                dot(self.goal[0], self.goal[1], rad, GOAL, ring=True)
                dot(self.goal[0], self.goal[1], max(2, rad // 3), GOAL)
            if self.robot:
                x, y, yaw = self.robot
                rad = max(5, int(0.12 / self.res * s))
                dot(x, y, rad, ROBOT)
                px, py = to_px(x, y)
                for t in np.linspace(0, rad * 2.2, int(rad * 3)):
                    qx, qy = int(px + t * math.cos(yaw)), int(py - t * math.sin(yaw))
                    if 0 <= qx < img.shape[1] and 0 <= qy < img.shape[0]:
                        img[max(0, qy - 1):qy + 2, max(0, qx - 1):qx + 2] = ROBOT
            return img

    def render_png(self):
        buf = io.BytesIO()
        plt.imsave(buf, self.render(), format='png')
        return buf.getvalue()

    def status(self):
        with self.lock:
            rng = self.noise_range()
            return {
                'has_map': self.has_map(),
                'nav': self.nav_state,
                'robot': [round(v, 2) for v in self.robot] if self.robot else None,
                'now_db': None if self.last_db is None else round(self.last_db, 1),
                'side': self.side_of(self.last_balance),
                'balance': None if self.last_balance is None else round(self.last_balance, 1),
                'loudest': None if self.loudest is None else round(self.loudest[2], 1),
                'readings': len(self.readings),
                'loc_age': None if self.last_loc is None else round(time.time() - self.last_loc, 1),
                'lo': None if not rng else round(rng[0], 1),
                'hi': None if not rng else round(rng[1], 1),
                'view': self.view,
            }

    def save(self, png_path, csv_path):
        with self.lock:
            if self.has_map():
                with open(png_path, 'wb') as f:
                    f.write(self.render_png())
            with open(csv_path, 'w') as f:
                f.write('time_s,x_m,y_m,heading_deg,noise_dB,left_dB,right_dB,balance_dB,side\n')
                t0 = self.readings[0][0] if self.readings else 0
                for t, x, y, yaw, db, dl, dr in self.readings:
                    bal = None if dl is None else dl - dr
                    f.write('%.2f,%.3f,%.3f,%.0f,%.1f,%s,%s,%s,%s\n' % (
                        t - t0, x, y, math.degrees(yaw), db,
                        '' if dl is None else '%.1f' % dl, '' if dr is None else '%.1f' % dr,
                        '' if bal is None else '%.1f' % bal, self.side_of(bal) or ''))


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Noise Navigator</title>
<style>
 body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;background:#f4f4f5;color:#18181b}
 header{padding:12px 16px;background:#18181b;color:#fff;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header h1{font-size:18px;margin:0}
 .wrap{display:flex;gap:16px;padding:16px;flex-wrap:wrap}
 .mapbox{position:relative;background:#fff;border-radius:8px;padding:8px;box-shadow:0 1px 3px #0002}
 #map{display:block;max-width:min(900px,calc(100vw - 48px));cursor:crosshair;image-rendering:pixelated}
 .side{flex:1;min-width:240px;max-width:340px;display:flex;flex-direction:column;gap:12px}
 .card{background:#fff;border-radius:8px;padding:12px 14px;box-shadow:0 1px 3px #0002}
 .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:#71717a;margin:0 0 8px}
 .row{display:flex;justify-content:space-between;padding:3px 0;font-size:14px}
 .row b{font-variant-numeric:tabular-nums}
 button{font:inherit;font-size:14px;padding:8px 12px;border-radius:6px;border:1px solid #d4d4d8;background:#fff;cursor:pointer;width:100%;margin-top:6px}
 button.on{background:#1f6feb;color:#fff;border-color:#1f6feb}
 button.stop{background:#dc2626;color:#fff;border-color:#dc2626}
 #nav{font-size:15px;font-weight:600}
 .bar{height:12px;border-radius:3px;background:linear-gradient(90deg,#ffffcc,#fed976,#fd8d3c,#e31a1c,#800026)}
 .ends{display:flex;justify-content:space-between;font-size:12px;color:#52525b;margin-top:3px}
 .key{display:flex;align-items:center;gap:8px;font-size:13px;padding:2px 0}
 .sw{width:14px;height:14px;border-radius:3px;display:inline-block;border:1px solid #0002}
 #hint{font-size:13px;color:#52525b}
</style></head><body>
<header><h1>Noise Navigator</h1><span id="nav">Connecting...</span></header>
<div class="wrap">
 <div class="mapbox"><img id="map" alt="map" src="/map.png"></div>
 <div class="side">
  <div class="card" id="loccard"><h2>Robot position</h2>
   <div id="loc" style="font-weight:600">-</div>
   <div id="lochint" style="font-size:13px;color:#52525b;margin-top:4px"></div>
  </div>
  <div class="card"><h2>Controls</h2>
   <div id="hint">Click a white spot on the map to send the robot there.</div>
   <button id="bGo" class="on">Go to (click map)</button>
   <button id="bPose">Set robot position</button>
   <button id="bCancel" class="stop">Stop / cancel goal</button>
   <button id="bClear">Clear obstacles (if stuck)</button>
   <button id="bSave">Save noise map</button>
  </div>
  <div class="card"><h2>Noise</h2>
   <div style="display:flex;gap:6px;margin-bottom:6px">
    <button id="vLoud" class="on" style="margin:0">Loudest</button>
    <button id="vAvg" style="margin:0">Average</button>
   </div>
   <div id="vhint" style="font-size:12px;color:#52525b;margin-bottom:6px">Each spot shows the loudest sound heard there (claps show up).</div>
   <div class="row"><span>Now</span><b id="now">-</b></div>
   <div class="row"><span>Direction</span><b id="side">-</b></div>
   <div class="row"><span>Loudest</span><b id="loud">-</b></div>
   <div class="row"><span>Readings</span><b id="cnt">0</b></div>
   <div style="margin-top:8px" class="bar"></div>
   <div class="ends"><span id="lo">quiet</span><span id="hi">loud</span></div>
  </div>
  <div class="card"><h2>Map key</h2>
   <div class="key"><span class="sw" style="background:#fff"></span>Free space (no noise yet)</div>
   <div class="key"><span class="sw" style="background:#191919"></span>Wall / obstacle</div>
   <div class="key"><span class="sw" style="background:#cdcdcd"></span>Unknown</div>
   <div class="key"><span class="sw" style="background:#1f6feb;border-radius:50%"></span>Robot</div>
   <div class="key"><span class="sw" style="background:#16a34a;border-radius:50%"></span>Goal</div>
   <div class="key"><span class="sw" style="background:#5aa0ff"></span>Planned path</div>
  </div>
  <div class="card"><h2>Robot</h2>
   <div class="row"><span>Position</span><b id="pos">-</b></div>
  </div>
 </div>
</div>
<script>
let mode='go', first=null;
const img=document.getElementById('map'), hint=document.getElementById('hint');
const $=id=>document.getElementById(id);
function setMode(m){mode=m;first=null;$('bGo').classList.toggle('on',m=='go');$('bPose').classList.toggle('on',m=='pose');
 hint.textContent=m=='go'?'Click a white spot on the map to send the robot there.':'Click where the robot really is, then click again in the direction it is facing.';}
$('bGo').onclick=()=>setMode('go'); $('bPose').onclick=()=>setMode('pose');
$('bCancel').onclick=()=>post('/api/cancel',{});
function setView(v){post('/api/view',{view:v});$('vLoud').classList.toggle('on',v=='loudest');$('vAvg').classList.toggle('on',v=='average');
 $('vhint').textContent=v=='loudest'?'Each spot shows the loudest sound heard there (claps show up).':'Each spot shows its typical (average) noise level.';}
$('vLoud').onclick=()=>setView('loudest'); $('vAvg').onclick=()=>setView('average');
$('bClear').onclick=()=>post('/api/clear',{}).then(r=>hint.textContent=r.msg||'Cleared');
$('bSave').onclick=()=>post('/api/save',{}).then(r=>hint.textContent=r.msg||'Saved');
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json()).catch(()=>({msg:'No connection'}));}
img.onclick=e=>{const r=img.getBoundingClientRect(); const fx=(e.clientX-r.left)/r.width, fy=(e.clientY-r.top)/r.height;
 if(mode=='go'){post('/api/goal',{fx,fy}).then(r=>{if(r.msg)hint.textContent=r.msg;});}
 else if(!first){first={fx,fy};hint.textContent='Now click in the direction the robot is facing.';}
 else{post('/api/pose',{fx:first.fx,fy:first.fy,fx2:fx,fy2:fy}).then(r=>{hint.textContent=r.msg||'Position set';setTimeout(()=>setMode('go'),1500);});}};
function refresh(){const n=new Image();n.onload=()=>{img.src=n.src;};n.src='/map.png?t='+Date.now();
 fetch('/api/status').then(r=>r.json()).then(s=>{$('nav').textContent=s.has_map?s.nav:'Waiting for the map...';
  $('now').textContent=s.now_db==null?'-':s.now_db+' dB';
  $('side').textContent=s.side==null?'-':(s.side=='centre'?'centre':s.side+' ('+(s.balance>0?'+':'')+s.balance+' dB)');$('loud').textContent=s.loudest==null?'-':s.loudest+' dB';
  $('cnt').textContent=s.readings;$('lo').textContent=s.lo==null?'quiet':s.lo+' dB';$('hi').textContent=s.hi==null?'loud':s.hi+' dB';
  $('pos').textContent=s.robot?('x '+s.robot[0]+'  y '+s.robot[1]):'-';
  const L=$('loc'),H=$('lochint'),C=$('loccard');
  if(s.loc_age==null){L.textContent='\u2717 Camera has not recognised the place yet';L.style.color='#dc2626';
   H.textContent='Turn the robot slowly on the spot (teleop) so the camera sees a mapped area, or use Set robot position.';C.style.outline='2px solid #dc2626';}
  else if(s.loc_age<30){L.textContent='\u2713 Camera knows where I am ('+Math.round(s.loc_age)+' s ago)';L.style.color='#16a34a';H.textContent='';C.style.outline='';}
  else{L.textContent='~ Last recognised '+Math.round(s.loc_age)+' s ago';L.style.color='#d97706';
   H.textContent='Position may have drifted. It will update when the camera sees a mapped area.';C.style.outline='';}}).catch(()=>{$('nav').textContent='Lost connection to robot';});}
setInterval(refresh,1000); refresh();
</script></body></html>"""


def make_handler(core, actions):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), 'application/json')

        def do_GET(self):
            p = self.path.split('?')[0]
            if p == '/':
                self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
            elif p == '/map.png':
                if not core.has_map():
                    blank = io.BytesIO()
                    plt.imsave(blank, np.full((300, 400, 3), 205, np.uint8), format='png')
                    self._send(200, blank.getvalue(), 'image/png')
                else:
                    self._send(200, core.render_png(), 'image/png')
            elif p == '/api/status':
                self._json(core.status())
            else:
                self._send(404, b'not found', 'text/plain')

        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                body = {}
            p = self.path
            if not core.has_map() and p in ('/api/goal', '/api/pose'):
                return self._json({'msg': 'No map yet'})
            if p == '/api/goal':
                x, y = core.frac_to_world(float(body['fx']), float(body['fy']))
                v = core.cell_value(x, y)
                if v < 0:
                    return self._json({'msg': 'That spot is unknown (grey) - pick a white spot.'})
                if v >= 50:
                    return self._json({'msg': 'That spot is a wall - pick a white spot.'})
                actions['goal'](x, y)
                return self._json({'msg': 'Going to x %.2f, y %.2f' % (x, y)})
            if p == '/api/pose':
                x, y = core.frac_to_world(float(body['fx']), float(body['fy']))
                x2, y2 = core.frac_to_world(float(body['fx2']), float(body['fy2']))
                yaw = math.atan2(y2 - y, x2 - x)
                actions['pose'](x, y, yaw)
                return self._json({'msg': 'Robot position set. Drive a little so it settles.'})
            if p == '/api/cancel':
                actions['cancel']()
                return self._json({'msg': 'Stopped'})
            if p == '/api/view':
                with core.lock:
                    core.view = 'average' if body.get('view') == 'average' else 'loudest'
                return self._json({'msg': 'View: ' + core.view})
            if p == '/api/clear':
                return self._json({'msg': actions.get('clear', lambda: 'Not available')()})
            if p == '/api/save':
                return self._json({'msg': actions['save']()})
            self._json({'msg': 'unknown'}, 404)
    return Handler


def start_server(core, actions, port=8000, host='127.0.0.1'):
    srv = ThreadingHTTPServer((host, port), make_handler(core, actions))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    import os
    import rospy
    import actionlib
    import tf2_ros
    from nav_msgs.msg import OccupancyGrid, Path
    from std_msgs.msg import Float32
    from geometry_msgs.msg import PoseWithCovarianceStamped
    from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
    from actionlib_msgs.msg import GoalStatus

    rospy.init_node('noise_nav_web')
    port = int(rospy.get_param('~port', 8000))
    base = rospy.get_param('~base_frame', 'base_footprint')
    plan_topic = rospy.get_param('~plan_topic', '/move_base/NavfnROS/plan')
    home = os.path.expanduser('~')
    png_path = rospy.get_param('~save_png', os.path.join(home, 'noise_nav_map.png'))
    csv_path = rospy.get_param('~save_csv', os.path.join(home, 'noise_nav_log.csv'))

    core = NoiseNavCore()

    use_clean = {'yes': False}

    def map_cb(m, clean=False):
        if clean:
            use_clean['yes'] = True
        elif use_clean['yes']:
            return                      # we have the cleaned map - ignore the raw one
        i = m.info
        core.set_map(m.data, i.width, i.height, i.resolution,
                     i.origin.position.x, i.origin.position.y)
        rospy.loginfo('Map received: %d x %d cells', i.width, i.height)

    def plan_cb(msg):
        with core.lock:
            core.path = [(p.pose.position.x, p.pose.position.y) for p in msg.poses]

    rospy.Subscriber('/map', OccupancyGrid, map_cb)
    rospy.Subscriber('/map_clean', OccupancyGrid, lambda m: map_cb(m, True))
    # /noise_reading = [overall dB, left dB, right dB, left-right dB] from noise_logger.py
    from std_msgs.msg import Float32MultiArray
    got_reading = {'yes': False}

    def reading_cb(m):
        got_reading['yes'] = True
        core.add_noise(m.data[0], left=m.data[1], right=m.data[2])

    def level_cb(m):                    # older noise_logger without /noise_reading
        if not got_reading['yes']:
            core.add_noise(m.data)
    rospy.Subscriber('/noise_reading', Float32MultiArray, reading_cb)
    rospy.Subscriber('/noise_level', Float32, level_cb)
    rospy.Subscriber(plan_topic, Path, plan_cb)
    # RTAB-Map (visual SLAM) publishes this whenever the camera recognises a place
    from geometry_msgs.msg import PoseWithCovarianceStamped as _PWC

    def loc_cb(msg):
        with core.lock:
            core.last_loc = time.time()
    rospy.Subscriber(rospy.get_param('~localization_topic', '/rtabmap/localization_pose'),
                     _PWC, loc_cb)
    pose_pub = rospy.Publisher('/initialpose', PoseWithCovarianceStamped, queue_size=1, latch=True)

    client = actionlib.SimpleActionClient('move_base', MoveBaseAction)
    ready = {'ok': False}

    def wait_server():
        core.nav_state = 'Waiting for move_base...'
        while not rospy.is_shutdown() and not client.wait_for_server(rospy.Duration(2.0)):
            rospy.logwarn_throttle(10, 'move_base not running yet (start run_vslam_noise_nav.sh)')
        ready['ok'] = True
        core.nav_state = 'Ready - click the map'
    threading.Thread(target=wait_server, daemon=True).start()

    def done_cb(state, result):
        names = {GoalStatus.SUCCEEDED: 'Arrived!',
                 GoalStatus.ABORTED: "Couldn't reach that spot - try another",
                 GoalStatus.PREEMPTED: 'Cancelled',
                 GoalStatus.RECALLED: 'Cancelled',
                 GoalStatus.REJECTED: 'Goal rejected'}
        with core.lock:
            core.nav_state = names.get(state, 'Stopped')
            core.path = []
            if state == GoalStatus.SUCCEEDED:
                core.goal = None

    from std_srvs.srv import Empty

    def clear():
        try:
            rospy.ServiceProxy('/move_base/clear_costmaps', Empty)()
            return 'Obstacle memory cleared - the LiDAR will re-detect real obstacles.'
        except Exception as e:
            return 'Could not clear: %s' % e

    def send_goal(x, y):
        if not ready['ok']:
            core.nav_state = 'move_base not ready yet'
            return
        clear()                          # forget old obstacles before each new goal
        yaw = 0.0
        if core.robot:
            yaw = math.atan2(y - core.robot[1], x - core.robot[0])
        g = MoveBaseGoal()
        g.target_pose.header.frame_id = 'map'
        g.target_pose.header.stamp = rospy.Time.now()
        g.target_pose.pose.position.x = x
        g.target_pose.pose.position.y = y
        g.target_pose.pose.orientation.z = math.sin(yaw / 2)
        g.target_pose.pose.orientation.w = math.cos(yaw / 2)
        with core.lock:
            core.goal = (x, y)
            core.nav_state = 'Driving to goal...'
        client.send_goal(g, done_cb=done_cb)
        rospy.loginfo('Goal sent: x=%.2f y=%.2f', x, y)

    def set_pose(x, y, yaw):
        p = PoseWithCovarianceStamped()
        p.header.frame_id = 'map'
        p.header.stamp = rospy.Time.now()
        p.pose.pose.position.x = x
        p.pose.pose.position.y = y
        p.pose.pose.orientation.z = math.sin(yaw / 2)
        p.pose.pose.orientation.w = math.cos(yaw / 2)
        p.pose.covariance[0] = p.pose.covariance[7] = 0.25
        p.pose.covariance[35] = 0.07
        pose_pub.publish(p)
        rospy.loginfo('Initial pose set: x=%.2f y=%.2f yaw=%.2f', x, y, yaw)

    def cancel():
        if ready['ok']:
            client.cancel_all_goals()
        with core.lock:
            core.goal = None
            core.path = []
            core.nav_state = 'Stopped'

    def save():
        core.save(png_path, csv_path)
        msg = 'Saved %s and %s (%d readings)' % (os.path.basename(png_path),
                                                os.path.basename(csv_path), len(core.readings))
        rospy.loginfo(msg)
        return msg

    srv = start_server(core, {'goal': send_goal, 'pose': set_pose,
                              'cancel': cancel, 'save': save, 'clear': clear}, port=port)
    rospy.loginfo('Noise Navigator running: on your PC open http://localhost:%d '
                  '(after ssh -L %d:localhost:%d ...)', port, port, port)

    tf_buf = tf2_ros.Buffer()
    tf2_ros.TransformListener(tf_buf)
    rate = rospy.Rate(10)
    while not rospy.is_shutdown():
        try:
            t = tf_buf.lookup_transform('map', base, rospy.Time(0))
            q = t.transform.rotation
            yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
            with core.lock:
                core.robot = (t.transform.translation.x, t.transform.translation.y, yaw)
        except Exception:
            pass
        try:
            rate.sleep()
        except rospy.ROSInterruptException:
            break

    if ready['ok']:
        client.cancel_all_goals()
    print(save())
    srv.shutdown()


if __name__ == '__main__':
    main()
