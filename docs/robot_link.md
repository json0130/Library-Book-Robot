# The Jetson–Dell link (`api_version` 1)

The robot has two computers. The **Jetson** is the brain: dialogue, screen, voice, the book logic. The **Dell laptop** rides on
the TurtleBot 2 and runs ROS Noetic: the Kobuki base, the Astra camera, the Hokuyo LiDAR, RTAB-Map, `move_base` and
`noise_logger` (everything in `turtlebot/`). This page is the contract between them. The Jetson side is
`robot/drivers/turtlebot_link.py`; the Dell side is `turtlebot/nodes/robot_link_api.py` (section 7), a small HTTP server that behaves as written here.
`tools/fake_base.py` is a working stand-in for the Dell, and `tests/test_robot_link_contract.py` is the test that tells you
whether a server keeps the contract.

```
 Jetson (brain)                                                                     Dell (on the TurtleBot, ROS Noetic)
 172.24.35.96  ── robot/drivers/turtlebot_link.py ── HTTP, JSON over Wi-Fi ───────  172.24.2.122:8010  (this API: turtlebot/nodes/robot_link_api.py)
 (both are DHCP addresses on the university Wi-Fi and can change;                   localhost:8000      (noise_nav_web.py, unchanged, reached with ssh -L)
  a direct Ethernet cable, section 5.2, is better for demos)
```

## 1. Rules for everything

| | |
|---|---|
| Transport | HTTP/1.1. **Today both computers are on the university Wi-Fi** (`172.24.0.0/18`): Jetson `172.24.35.96`, Dell `172.24.2.122`. Those are DHCP addresses and can change, so no address is written in any code: on the Jetson it is `turtlebot.base_url` in `config/robot.yaml`; on the Dell the API binds to the address of its Wi-Fi interface (`_iface`, default `wlp2s0`), looked up each time it starts (or an explicit `_host`). The API is on port **8010** (the click-to-go page of `noise_nav_web.py` stays on 8000, on the Dell's localhost). For demos use a direct Ethernet cable (section 5.2): no one else is on it. |
| Format | JSON only (`Content-Type: application/json`), UTF-8. A request body is one JSON object, at most 16 KB. |
| Auth | Every request carries the header `X-Robot-Token: <secret>`. The secret is the environment variable `ROBOT_LINK_TOKEN` on both machines; it is never written to a config file or the repository. A missing or wrong token is `401`, and the Dell refuses to start without one. **Why this matters on a shared network:** anyone on the Wi-Fi can open a connection to port 8010, and `POST /api/goal` makes a robot with wheels drive. The token is the only thing that stops them, so use a long random one (`openssl rand -hex 16`) and change it if it ever leaks. It is sent in clear text over plain HTTP, so on a shared network someone capturing packets could read it: another reason to use the direct cable for demos. |
| Frame and units | Every pose is in the RTAB-Map **`map`** frame: metres and radians (yaw `0` along +x, counter-clockwise positive, `-π..π`). The same numbers are in `data/shelves.csv` (`map_x, map_y, map_yaw`) and `data/floorplan.yaml`; `tools/teach_poses.py` records them from the live pose. |
| Time | UNIX seconds as a float (`time.time()`). The two clocks only need to agree within about a second: on the Wi-Fi both already follow internet time (`systemd-timesyncd`). On the direct cable, where there is no internet, chrony with the Jetson as the source does it (section 5.2). |
| Timeouts | The Jetson waits **1 s** for each request. The Dell must answer every endpoint well inside that, and never block on ROS: answer from the latest values it holds. |
| Versioning | `/api/health` says `api_version`. The Jetson refuses to drive a base that reports another version than the one it was written for. New optional fields may be added to a response without a version change; a client ignores fields it does not know. |

### Errors

A request that could not be handled answers with a non-2xx status and always this body:

```json
{"ok": false, "error": "bad_request", "detail": "x must be a number"}
```

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | not JSON, not an object, a field missing or the wrong type, a number that is not finite |
| 401 | `unauthorised` | missing or wrong `X-Robot-Token` |
| 404 | `not_found` | no such path (this is also the right answer for an optional endpoint the Dell does not offer) |
| 405 | `method_not_allowed` | `GET` on a `POST` path or the other way round |
| 413 | `payload_too_large` | body over 16 KB |
| 500 | `internal` | a bug on the Dell; the Jetson logs it and counts the link as degraded |
| 503 | `ros_unavailable` | ROS (or `move_base`) is not running, so the request cannot be done |

A request that was understood but **refused for a reason the robot has to act on** is not an error: it answers `200` with
`accepted: false` and a `reason` (see `/api/goal`).

## 2. Endpoints

### `GET /api/health`

```json
{"ok": true, "api_version": 1, "ros_ok": true, "localised": true, "battery": 0.82, "time": 1761730000.12}
```

- `ros_ok`: `move_base` is publishing its status (< 3 s old), the base is publishing odometry (< 2 s old) and the RTAB-Map node is registered with the ROS master. While `false` every goal is refused with `503`. It is `false` too when ROS is not running at all (the server then still answers).
- `localised`: the base knows where it is on the map. **The rule** (all of it must hold):
  1. the `map` -> `base_footprint` transform is fresh (younger than `tf_max_age_s`, 1 s); without it there is no pose at all;
  2. RTAB-Map has localised at least once since the API started (`/rtabmap/localization_pose` has arrived);
  3. the last `/rtabmap/localization_pose` is younger than `loc_max_age_s`, **or** the wheel odometry shows the robot has not moved more than 5 cm or turned more than 5 degrees since that localisation (a robot that stands still is not lost, and RTAB-Map may well stay quiet then).
  While a goal is active and the base has been not localised for `lost_localisation_after_s` (5 s), the goal is stopped and fails with `lost_localisation`.
- `battery`: 0..1 from the Kobuki's battery voltage (`/mobile_base/sensors/core`, linear between 13.2 V and 16.5 V: a rough guide, the voltage sags under load), or `null` if unknown.
- Must answer even when ROS is down (then `ros_ok: false`).

### `GET /api/state`

```json
{
  "pose":  {"x": 3.21, "y": 1.05, "yaw": 1.57, "frame": "map", "localised": true, "t": 1761730000.10},
  "nav":   {"status": "active", "goal_id": "g-7f3a-12", "distance_left": 2.4, "reason": ""},
  "noise": {"overall": 41.5, "left": 41.0, "right": 42.1, "balance": -1.1, "unit": "dB", "calibrated": true,
            "robot_noise": false, "t": 1761730000.00},
  "time": 1761730000.12
}
```

- `pose`: the `map` -> `base_footprint` transform of tf (RTAB-Map's correction on top of the wheel odometry), `t` is the transform's time. While `localised` is `false` the numbers are the last known ones (zeros before the first) and must not be trusted.
- `nav.status`: `idle` (no goal since the server started), `active`, `succeeded`, `failed`, `cancelled`.
  A finished goal **stays reported** (same `goal_id`, final status) until another goal arrives, so the Jetson cannot miss the outcome by polling between two goals.
  `distance_left` is metres along the plan to the goal (`null` when not `active`, or not known). `reason` is empty unless the goal `failed` or was `cancelled`:
  `no_path`, `blocked`, `aborted`, `timeout`, `lost_localisation`, `ros_unavailable` (failed); `cancelled_by_client`, `superseded`, `heartbeat_timeout`, `manual_override` (cancelled).
  `manual_override`: someone sent another goal, or pressed Stop, on the click-to-go page (port 8000) while the Jetson's goal was active. The person wins: the Dell does not fight them, and the Jetson reads its goal as cancelled and must not resend it by itself.
- `noise`: the latest reading of `noise_logger` (`/noise_reading`: overall, left, right, balance). `overall` is the level the Jetson uses.
  - `unit`: `"dB"` (a calibrated sound level) or `"dBFS"` (decibels relative to the microphone's full scale, always negative, **not** comparable with the library's 45 / 60 dB thresholds).
  - `calibrated`: `true` only when an offset against a reference sound meter has been applied on the Dell. `false` means `overall` is a raw `dBFS` value.
  - `robot_noise`: `true` if the robot itself was making noise: `speaking` was true, or the wheels were turning (odometry above 2 cm/s or 3 degrees/s), at any time in the second before the reading arrived (the half second it measured plus 0.5 s afterwards). The Jetson ignores such readings. The Dell's noise logger and noise map skip them too.
  - `t`: when the reading was taken. A new reading arrives about every 0.5 s; the Jetson ignores a repeated `t`.
  - If no reading arrived for 3 s (the microphones are not running), `noise` is `null`.

### `POST /api/goal`

Request: `{"goal_id": "g-7f3a-12", "x": 3.2, "y": 1.0, "yaw": 1.57}`

Response `200`: `{"ok": true, "accepted": true, "goal_id": "g-7f3a-12", "reason": ""}`

- **The same `goal_id` twice is the same goal.** The Dell remembers the last 32 ids. A repeat answers `accepted: true` with `"duplicate": true` and changes nothing, so the Jetson may resend after a lost reply.
- A **different** `goal_id` while another goal is active replaces it: the old goal ends `cancelled` with reason `superseded`, and the base drives to the new one without stopping in between if it can.
- Refused (`accepted: false`, still `200`), with `reason`:
  - `not_localised`: RTAB-Map has no trusted pose. **A base that is not localised never drives**: it would be driving blind. The Jetson treats this as "navigation unavailable" (it waits) and does not count it as a failed shelf.
  - `no_heartbeat`: no heartbeat in the last 3 s (see below). The Jetson retries after a moment.
  - `invalid_pose`: not a number, or yaw outside `-π..π` by more than a rounding error.
  - `outside_map`: the point is outside the known map or inside an obstacle that is known and permanent.
  - `ros_unavailable`: also answered with status `503` as the error above.

### `POST /api/cancel`

Request: `{"goal_id": "g-7f3a-12"}` — or `{"goal_id": "*"}` to cancel whatever is active (the Jetson sends this once at start-up, in case it crashed during a drive).

Response `200`: `{"ok": true, "cancelled": true}`. `cancelled` is `false` if that goal is not the active one (already finished, never seen): not an error. The base stops at once; the goal is reported `cancelled` with reason `cancelled_by_client`. Idempotent.

### `POST /api/speaking`

Request: `{"speaking": true}` → `{"ok": true}`. Idempotent: it sets a flag, it does not count.
While the flag is true (and for 1 s after it, see `robot_noise`) readings carry `robot_noise: true`. The Jetson sets it to true when its speaker starts playing speech or the chime and to false when it stops. The Dell publishes it as `/robot_speaking` (`std_msgs/Bool`, latched) for the noise logger and the noise map.
If the Jetson stops talking to the Dell while the flag is true, the Dell clears it after 30 s on its own.

### `POST /api/heartbeat`

Request: `{"t": 1761730000.12}` → `{"ok": true, "time": 1761730000.13}`. The Jetson sends one **every second**, whether or not a goal is active.

**The safety rule: if the Dell has a goal `active` and receives no heartbeat for 3 s, it stops the base and cancels the goal (reason `heartbeat_timeout`).**
A cable pulled out, the Jetson crashing or rebooting, or a stalled network therefore always stops the robot within 3 s. After that the Dell refuses goals (`no_heartbeat`) until a heartbeat arrives again. The Dell does not start driving again by itself when the heartbeat returns: the Jetson sends a new goal.

### `POST /api/max_speed` (optional)

Request: `{"m_s": 0.3}` → `{"ok": true, "applied": 0.25}`. The Jetson sends the interaction style's speed limit whenever the style changes (0.3 / 0.5 / 0.6 m/s). The Dell applies `min(m_s, its own limit)` to the DWA planner's `max_vel_x` (dynamic_reconfigure on `/move_base/DWAPlannerROS`; the limit is `_max_speed_limit`, 0.25 m/s as in `turtlebot/config/hokuyo_costmap.yaml`; the velocity smoother's 0.8 m/s is not touched) and reports what it applied. It never goes below 0.05 m/s. `404` means this base cannot do it (the local planner is not DWA, or it is switched off): the Jetson then never asks again. `503` means `move_base` is not running right now: the Jetson asks again when the link comes back. The original `max_vel_x` is put back when the API stops.

## 3. What the Jetson does with it

- Starts the link in the background (it never blocks the robot's event loop): a heartbeat every 1 s and a state poll every 0.5 s on one thread, and commands (goal, cancel, speaking, max_speed) on another, in order, with up to 2 retries (they are idempotent). `GET /api/health` is read every 5 s.
- **Link status**: *connected* (the last request worked), *degraded* (some requests failed in the last 3 s), *lost* (3 s without any reply). Entering *lost* publishes `LinkDown`; the first reply after that publishes `LinkUp`.
- **A goal** gets a new `goal_id` each time it is sent. The outcome is read from `nav.status` of `/api/state` for that id and published once: `NavArrived`, `NavFailed` (with the `reason`) or `NavCancelled`. While `active`, `NavProgress` carries `distance_left`.
- A goal the Jetson sent but the Dell never confirmed is resent (same id) until it is accepted, the link is lost or another goal replaces it.
- When nothing answers at all the link goes *lost* with the reason `base unreachable at <url>: check the Dell's IP (its Wi-Fi address can change; see turtlebot.base_url) and that robot_link_api is running there`.
- **Never sent**: a goal outside the floor plan's bounds, or further from the base than `turtlebot.max_goal_distance_m`. It is rejected on the Jetson, logged, and counted as failed.

## 4. Behaviour you can test without hardware

```
python tools/fake_base.py --port 8010 --token $ROBOT_LINK_TOKEN          # the Dell stand-in
python -m robot sim --base turtlebot --base-url http://127.0.0.1:8010    # the robot using it
python -m pytest tests/test_robot_link_contract.py                       # the contract, against the fake
python -m pytest tests/test_robot_link_contract.py --target http://172.24.2.122:8010   # ...against the real Dell (read-only checks)
python -m pytest tests/test_robot_link_contract.py --target http://172.24.2.122:8010 --drive   # ...and the goals: the base MOVES
(cd turtlebot && ROBOT_LINK_TOKEN=x python3 -m robot_link.dev_server --port 8010)   # the Dell's real server over a simulated base, no ROS
```

The contract tests on a real base must not drive it: with `--target` they only run the read-only checks, authentication, validation errors,
the heartbeat, speaking and max_speed, and goal / cancel only when `--drive` is added (the base then moves a little: a goal 0.3 m ahead of where it stands).

## 5. Network, clock and secret

### 5.1 Today: the university Wi-Fi (nothing to configure)

Both computers are on `172.24.0.0/18` and reach each other. The Dell's address comes from DHCP and can change when it reconnects or reboots,
so it is never written in code:

```bash
# on the Dell: the address robot_link_api will bind to (and does, at every start); this is what turtlebot.base_url needs
python3 ~/lbr-dell/turtlebot/nodes/robot_link_api.py --print-address            # e.g. 172.24.2.122 (the wlp2s0 interface)

# on the Jetson: put it in config/robot.yaml
#   turtlebot:
#     base_url: http://172.24.2.122:8010
getent hosts <dell-hostname>.local        # a name would survive an address change, but mDNS does not resolve here today
```

If the address changes while the API runs, it moves to the new one by itself within 10 s and logs it; the Jetson's link then reads *lost*
with "base unreachable ... check the Dell's IP" until `turtlebot.base_url` is updated. The Dell's own interface (`_iface`, default `wlp2s0`) or an
explicit address (`_host`) are command-line parameters of the node (section 7).

**Clocks:** both machines follow internet time through `systemd-timesyncd` already; that is far closer than the 1 s the link needs. Nothing to do.

**Port 8010 on a shared network:** anyone on the Wi-Fi can reach it; only the token stops them (section 1, Auth). The Dell has no firewall rules for it
(`ufw` is not managed from here); do not use this setup in a public place.

### 5.2 For demos: a direct Ethernet cable (recommended; needs `sudo` once)

No one else is on the cable, nothing depends on DHCP and the Wi-Fi's quality, and nothing can be sniffed. The Dell (a laptop) may need a USB-Ethernet
adapter: it has no wired interface today. Interface names differ per machine: find them with `nmcli device status` (the wired one is `ethernet`,
e.g. `eno1` on the Dell, `enP8p1s0` on a Jetson). Below `JETSON_IFACE` and `DELL_IFACE` stand for them.

**Jetson (Ubuntu 24.04):**

```bash
nmcli device status                                                     # note the wired interface
sudo nmcli connection add type ethernet ifname JETSON_IFACE con-name robot-link \
     ipv4.method manual ipv4.addresses 192.168.50.1/24 ipv4.never-default yes ipv6.method disabled \
     connection.autoconnect yes
sudo nmcli connection up robot-link
```

**Dell (Ubuntu 20.04):**

```bash
nmcli device status
sudo nmcli connection add type ethernet ifname DELL_IFACE con-name robot-link \
     ipv4.method manual ipv4.addresses 192.168.50.2/24 ipv4.never-default yes ipv6.method disabled \
     connection.autoconnect yes
sudo nmcli connection up robot-link
sudo ufw allow from 192.168.50.1 to any port 8010 proto tcp             # only if ufw is on
```

`ipv4.never-default yes` keeps the cable from becoming the default route: each machine keeps using Wi-Fi for the internet.
Then start the API on the cable's address (`_iface:=DELL_IFACE`, or `_host:=192.168.50.2`) and set `turtlebot.base_url: http://192.168.50.2:8010` on the Jetson.

**Clocks on the cable (chrony, the Jetson is the time source).** With no internet the clocks drift apart; timestamps and the 3 s watchdog need them to agree to well under a second.

```bash
# Jetson: serve its clock to the cable, even when it has no internet
sudo apt install chrony
printf 'allow 192.168.50.0/24\nlocal stratum 10\n' | sudo tee /etc/chrony/conf.d/robot-link.conf
sudo systemctl restart chrony

# Dell: take the time from the Jetson only
sudo apt install chrony
sudo sed -i 's/^pool /#pool /; s/^server /#server /' /etc/chrony/chrony.conf
echo 'server 192.168.50.1 iburst prefer minpoll 0 maxpoll 4' | sudo tee -a /etc/chrony/chrony.conf
echo 'makestep 1.0 3' | sudo tee -a /etc/chrony/chrony.conf
sudo systemctl restart chrony
```

### 5.3 The shared secret

The same string on both machines, in the environment of the robot process and of the Dell's API server. Never in the repository, never in a config file.

```bash
openssl rand -hex 16                                                   # on the Jetson: make one, and keep the output
export ROBOT_LINK_TOKEN=<that value>                                    # on the Jetson, in the shell (or its service) that runs the robot
# on the Dell: one line in ~/lbr-dell/turtlebot/.robot_link_env (ignored by git), readable only by its owner; run_robot_link.sh refuses a file others can read
printf 'export ROBOT_LINK_TOKEN=%s\n' '<that value>' > ~/lbr-dell/turtlebot/.robot_link_env && chmod 600 ~/lbr-dell/turtlebot/.robot_link_env
```

### 5.4 Test it, in this order

```bash
ping -c 3 172.24.2.122                                        # from the Jetson: reachable (on the cable: 192.168.50.2)
curl -s -H "X-Robot-Token: $ROBOT_LINK_TOKEN" http://172.24.2.122:8010/api/health      # from the Jetson
curl -s -H "X-Robot-Token: $ROBOT_LINK_TOKEN" http://172.24.2.122:8010/api/state
curl -s -o /dev/null -w '%{http_code}\n' http://172.24.2.122:8010/api/health           # no token: 401
chronyc tracking                                              # cable only, on the Dell: "System time" within 0.05 seconds of NTP time
```

The robot also compares the Dell's `time` with its own on every health check and logs a warning if they differ by more than 1 s.

## 6. Mapping onto what runs on the Dell

| API | ROS / existing code |
|---|---|
| `pose` | tf `map` -> `base_footprint` (RTAB-Map publishes `map` -> `odom`, the Kobuki driver `odom` -> `base_footprint`); `localised` is the rule in section 2 |
| `POST /api/goal` | the `move_base` action client (`MoveBaseGoal` in the `map` frame, after `/move_base/clear_costmaps`, like `send_goal` in `noise_nav_web.py`) |
| `nav` | the action's result: `SUCCEEDED` -> `succeeded`, `ABORTED`/`REJECTED` -> `failed`, `PREEMPTED`/`RECALLED` not by us -> `cancelled` (`manual_override`); `distance_left` along `/move_base/NavfnROS/plan` (this stack uses Navfn, not GlobalPlanner) to the goal, else in a straight line |
| `POST /api/cancel`, watchdog | `cancel_goal()` of our own goal (the click-to-go page's goals are not touched) and zero `Twist` on `/navigation_velocity_smoother/raw_cmd_vel` and `/cmd_vel_mux/input/navi` |
| `noise` | `/noise_reading` (`[overall, left, right, balance]` in dBFS) from `noise_logger.py`; with `~/noise_calibration.json` the API adds the offsets and reports `dB` |
| `robot_noise` | `/robot_speaking` (`Bool`, our own flag) and `/odom` twist |
| `POST /api/max_speed` | dynamic_reconfigure `/move_base/DWAPlannerROS`, `max_vel_x` |
| `battery` | `/mobile_base/sensors/core` (`kobuki_msgs/SensorState.battery`, tenths of a volt) |
| `outside_map` | `/map_clean` (else `/map`): outside the grid, or a cell that is a known obstacle |

`noise_logger.py` publishes dBFS (`20·log10(rms/32768)`, minus the left-microphone offset in `mic_config.json`). Until `calibrate_noise.py` has measured the same sounds against a phone sound meter and saved `~/noise_calibration.json`, the Dell reports `unit: "dBFS"`, `calibrated: false`; the Jetson then follows `noise.uncalibrated` in `config/zones.yaml` (ignore the noise, or compare against `noise.raw_thresholds`).

## 7. The Dell's implementation (`turtlebot/`)

| File | |
|---|---|
| `nodes/robot_link_api.py` | the node: HTTP on port 8010, connected to ROS in a thread (the server answers even with no ROS master) |
| `robot_link/` | the logic in plain Python 3.8 with no ROS (`core.py` the API's behaviour, `server.py` HTTP, `rules.py`, `activity.py`, `calibration.py`, `netaddr.py`); `dev_server.py` serves it over a simulated base |
| `calibrate_noise.py` | gain / AGC check and the guided 3-level calibration against a phone |
| `measure_localisation.py` | listens to `/rtabmap/localization_pose` standing still and moving, suggests `_loc_max_age_s` |
| `run_robot_link.sh` | starts the whole stack including the API; refuses if another ROS stack is running |
| `tests/` | `python3 -m unittest discover -s tests` (no ROS needed) |

Node parameters, ROS style on the command line (`python3 nodes/robot_link_api.py _iface:=wlp2s0 _port:=8010`; they work without a ROS master, which is why they are not read from the parameter server):
`_iface` (default `wlp2s0`), `_host` (overrides `_iface`; `127.0.0.1` for tests), `_port`, `_max_speed_limit` (0.25), `_tf_max_age_s` (1.0), `_loc_max_age_s`, `_still_dist_m` (0.05), `_still_turn_deg` (5), `_lost_localisation_after_s` (5), `_goal_timeout_s` (900),
`_calibration_file`, `_skip_clear_costmaps`. `--print-address` prints the address it would bind to and exits.

The watchdog and every other timer run in a 10 Hz loop and before each request. When a goal is cancelled by us (client, watchdog, lost localisation, shutdown) the node cancels its own `move_base` goal and sends zero velocity for half a second.
If the ROS master restarts, the node re-executes itself to reconnect (rospy cannot re-register).
