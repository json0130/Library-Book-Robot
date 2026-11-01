# The Jetson–Dell link (`api_version` 1)

The robot has two computers. The **Jetson** is the brain: dialogue, screen, voice, the book logic. The **Dell laptop** rides on
the TurtleBot 2 and runs ROS Noetic: the Kobuki base, the Astra camera, the Hokuyo LiDAR, RTAB-Map, `move_base` and
`noise_logger` (everything in `turtlebot/`). This page is the contract between them. The Jetson side is
`robot/drivers/turtlebot_link.py`; the Dell side is a small HTTP server (built separately) that must behave as written here.
`tools/fake_base.py` is a working stand-in for the Dell, and `tests/test_robot_link_contract.py` is the test that tells you
whether a server keeps the contract.

```
 Jetson (brain)                         direct Ethernet cable                      Dell (on the TurtleBot, ROS Noetic)
 192.168.50.1  ── robot/drivers/turtlebot_link.py ─────────  HTTP, JSON  ─────────  192.168.50.2:8010  (this API)
                                                                                    192.168.50.2:8000  (noise_nav_web.py, unchanged)
```

## 1. Rules for everything

| | |
|---|---|
| Transport | HTTP/1.1 over the direct cable. Jetson `192.168.50.1`, Dell `192.168.50.2`. The API is on port **8010** (the click-to-go page of `noise_nav_web.py` stays on 8000). |
| Format | JSON only (`Content-Type: application/json`), UTF-8. A request body is one JSON object, at most 16 KB. |
| Auth | Every request carries the header `X-Robot-Token: <secret>`. The secret is the environment variable `ROBOT_LINK_TOKEN` on both machines; it is never written to a config file or the repository. A missing or wrong token is `401`. |
| Frame and units | Every pose is in the RTAB-Map **`map`** frame: metres and radians (yaw `0` along +x, counter-clockwise positive, `-π..π`). The same numbers are in `data/shelves.csv` (`map_x, map_y, map_yaw`) and `data/floorplan.yaml`; `tools/teach_poses.py` records them from the live pose. |
| Time | UNIX seconds as a float (`time.time()`). The two clocks are kept together by chrony (section 5). |
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

- `ros_ok`: `move_base`, RTAB-Map and the base driver are all up. While `false` every goal is refused.
- `localised`: RTAB-Map has a pose it trusts on the loaded map (`rtabmap/localization_pose` is recent and the covariance is small).
- `battery`: 0..1, or `null` if unknown.
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

- `pose`: from `/rtabmap/localization_pose`. While `localised` is `false` the numbers are the last known ones and must not be trusted.
- `nav.status`: `idle` (no goal since the server started), `active`, `succeeded`, `failed`, `cancelled`.
  A finished goal **stays reported** (same `goal_id`, final status) until another goal arrives, so the Jetson cannot miss the outcome by polling between two goals.
  `distance_left` is metres along the plan to the goal (`null` when not `active`, or not known). `reason` is empty unless the goal `failed` or was `cancelled`:
  `no_path`, `blocked`, `aborted`, `timeout`, `lost_localisation`, `ros_unavailable` (failed); `cancelled_by_client`, `superseded`, `heartbeat_timeout` (cancelled).
- `noise`: the latest reading of `noise_logger` (`/noise_reading`: overall, left, right, balance). `overall` is the level the Jetson uses.
  - `unit`: `"dB"` (a calibrated sound level) or `"dBFS"` (decibels relative to the microphone's full scale, always negative, **not** comparable with the library's 45 / 60 dB thresholds).
  - `calibrated`: `true` only when an offset against a reference sound meter has been applied on the Dell. `false` means `overall` is a raw `dBFS` value.
  - `robot_noise`: `true` if the reading was taken while the robot itself was making noise (the `speaking` flag is true, or just ended, on the Dell's choice of grace period; or the base is driving fast). The Jetson ignores such readings.
  - `t`: when the reading was taken. A new reading arrives about every 0.5 s; the Jetson ignores a repeated `t`.
  - If the microphones are not running, `noise` is `null`.

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
While the flag is true (and for a short grace period the Dell chooses, about 0.5 s) readings carry `robot_noise: true`. The Jetson sets it to true when its speaker starts playing speech or the chime and to false when it stops.
If the Jetson stops talking to the Dell while the flag is true, the Dell clears it after 30 s on its own.

### `POST /api/heartbeat`

Request: `{"t": 1761730000.12}` → `{"ok": true, "time": 1761730000.13}`. The Jetson sends one **every second**, whether or not a goal is active.

**The safety rule: if the Dell has a goal `active` and receives no heartbeat for 3 s, it stops the base and cancels the goal (reason `heartbeat_timeout`).**
A cable pulled out, the Jetson crashing or rebooting, or a stalled network therefore always stops the robot within 3 s. After that the Dell refuses goals (`no_heartbeat`) until a heartbeat arrives again. The Dell does not start driving again by itself when the heartbeat returns: the Jetson sends a new goal.

### `POST /api/max_speed` (optional)

Request: `{"m_s": 0.3}` → `{"ok": true, "applied": 0.25}`. The Jetson sends the interaction style's speed limit whenever the style changes (0.3 / 0.5 / 0.6 m/s). The Dell applies `min(m_s, its own hardware limit)` to `move_base` (the DWA planner in `turtlebot/config/hokuyo_costmap.yaml` is limited to 0.25 m/s today) and reports what it applied. A Dell that does not offer it answers `404`; the Jetson then never asks again.

## 3. What the Jetson does with it

- Starts the link in the background (it never blocks the robot's event loop): a heartbeat every 1 s and a state poll every 0.5 s on one thread, and commands (goal, cancel, speaking, max_speed) on another, in order, with up to 2 retries (they are idempotent). `GET /api/health` is read every 5 s.
- **Link status**: *connected* (the last request worked), *degraded* (some requests failed in the last 3 s), *lost* (3 s without any reply). Entering *lost* publishes `LinkDown`; the first reply after that publishes `LinkUp`.
- **A goal** gets a new `goal_id` each time it is sent. The outcome is read from `nav.status` of `/api/state` for that id and published once: `NavArrived`, `NavFailed` (with the `reason`) or `NavCancelled`. While `active`, `NavProgress` carries `distance_left`.
- A goal the Jetson sent but the Dell never confirmed is resent (same id) until it is accepted, the link is lost or another goal replaces it.
- **Never sent**: a goal outside the floor plan's bounds, or further from the base than `turtlebot.max_goal_distance_m`. It is rejected on the Jetson, logged, and counted as failed.

## 4. Behaviour you can test without hardware

```
python tools/fake_base.py --port 8010 --token $ROBOT_LINK_TOKEN          # the Dell stand-in
python -m robot sim --base turtlebot --base-url http://127.0.0.1:8010    # the robot using it
python -m pytest tests/test_robot_link_contract.py                       # the contract, against the fake
python -m pytest tests/test_robot_link_contract.py --target http://192.168.50.2:8010   # ...against the real Dell
```

The contract tests on a real base must not drive it: with `--target` they only run the read-only checks, authentication, validation errors,
the heartbeat, speaking and max_speed, and goal / cancel only when `--drive` is added (the base then moves a little: a goal 0.3 m ahead of where it stands).

## 5. Network and clock setup (once, needs `sudo`)

Interface names differ per machine: find them with `nmcli device status` (the wired one is `ethernet`, e.g. `eno1` on the Dell, `enP8p1s0` on a Jetson). Below `JETSON_IFACE` and `DELL_IFACE` stand for them.

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

**Clocks (chrony, the Jetson is the time source).** Timestamps and the 3 s watchdog only make sense if both clocks agree to well under a second.

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

**The shared secret** (the same string on both machines, in the environment of the robot process and of the Dell's API server):

```bash
export ROBOT_LINK_TOKEN="$(openssl rand -hex 16)"; echo "$ROBOT_LINK_TOKEN"     # on the Jetson; copy the value to the Dell
echo "export ROBOT_LINK_TOKEN=<that value>" >> ~/.bashrc                        # on both
```

**Test it, in this order:**

```bash
ping -c 3 192.168.50.2                                        # from the Jetson: the cable and the addresses
chronyc tracking                                              # on the Dell: "System time" within 0.05 seconds of NTP time
chronyc sources                                               # on the Dell: 192.168.50.1 marked ^* (selected)
curl -s -H "X-Robot-Token: $ROBOT_LINK_TOKEN" http://192.168.50.2:8010/api/health      # from the Jetson
curl -s -H "X-Robot-Token: $ROBOT_LINK_TOKEN" http://192.168.50.2:8010/api/state
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.50.2:8010/api/health           # no token: 401
```

The robot also compares the Dell's `time` with its own on every health check and logs a warning if they differ by more than 1 s.

## 6. Mapping onto what already runs on the Dell (for whoever builds the server)

| API | ROS / existing code |
|---|---|
| `pose` | `/rtabmap/localization_pose` (the same topic `noise_nav_web.py` already subscribes to); `localised` is "recent, small covariance" |
| `POST /api/goal` | the `move_base` action client of `noise_nav_web.py` (`send_goal` in the `map` frame, after `/move_base/clear_costmaps`) |
| `nav` | the action's status (`SUCCEEDED`, `ABORTED` → `failed`, `PREEMPTED` → `cancelled`) and `distance_left` from `/move_base/GlobalPlanner/plan` or the pose |
| `POST /api/cancel`, watchdog | `cancel_goal()` and zero `cmd_vel` |
| `noise` | `/noise_reading` (`[overall, left, right, balance]`) from `noise_logger.py`, plus the calibration offset; `robot_noise` from the `speaking` flag |
| `POST /api/max_speed` | `move_base` dynamic reconfigure of the DWA planner (`max_vel_x`, `max_vel_trans`) |

`noise_logger.py` publishes dBFS today (`20·log10(rms/32768)`, minus the left-microphone offset). Until someone measures the same sound with a reference sound meter and stores an offset, the Dell must report `unit: "dBFS"`, `calibrated: false`; the Jetson then follows `noise.uncalibrated` in `config/zones.yaml` (ignore the noise, or compare against `noise.raw_thresholds`).
