# Sound calibration and the sound map

How loud it is, square by square, on the same map as the floor plan. Two separate panels on the operator page: **Floor plan and map** (walls, sections, keep-out zones, shelves; you can edit zones and sections there)
and **Sound map** (the same walls and the sections as dashed outlines, with the sound level blended smoothly over the floor: dark blue = quiet,
green = middle, red = loud; the colours fade out away from the places where the robot listened, so faded means "not measured here").

Nothing here starts by itself. The steps that make the robot drive are marked **DRIVES**: do them only with the area clear and someone at
the power switch.

## What was found on 2026-11-17 (before you start)

- **The two USB microphones are not plugged in** (`arecord -l` on the Dell lists only the camera's and the laptop's). The noise logger therefore
  writes nothing ("USB mic not found on port 1-2.6 / 1-2.7").
- **The old mic config expects the right microphone on hub port 1-2.7, which is now the LiDAR's.** The hub (`lsusb -t`, bus 1 port 2) has 7
  ports: port 4 is the Astra camera, port 7 the Hokuyo LiDAR. Free: 1, 2, 3, 5, 6.
- `~/mic_config.json` on the Dell belongs to another person and is never edited. The scripts now accept `MIC_CONFIG=<file>`, and
  `run_robot_link.sh` uses `turtlebot/run_data/mic_config.json` when it exists, so ours is separate.
- **No calibration yet** (`~/noise_calibration.json` is missing): the levels would be dBFS, only good for comparing places.
- **The LiDAR map has never seen the right half of the room** (about 60 % of it for x 1 to 4 m is "unknown", shadowed behind the desks). The
  survey only plans points on ground the map knows, so that half gets no points until the map is extended.

## What was found on 2026-11-18 (the microphones)

- Both USB mics work: left = hub port 6 (`1-2.6`, "USB PnP Sound Device", gain range 0-16, auto gain was ON), right = hub port 5 (`1-2.5`, UACDemoV1.0,
  gain range 0-147). `setup_mics.py` told them apart (15 and 23 dB), `run_data/mic_config.json` is saved, auto gain on the left is now off
  (`amixer -c 0 cset name='Auto Gain Control' off`).
- **The dB calibration does not work with these microphones.** The phone-meter calibration (`calibrate_noise.py`) was refused three times (error 13 dB,
  slope 2.1 to 5.8 instead of 1.0) and nothing was saved. The reason: both mics have a noise floor of about 60 dB on the phone.
  - Right mic: reads a constant -48.6 dBFS in a quiet room and with the phone's 60 dB sound, at gain 35 %, 69 % and 100 % alike. It only moves at about 70 dB.
  - Left mic: its floor rises with its gain (-50 dBFS at gain 0, -41 at 8 of 16, -30 at 16), so more gain does not help. At gain 8 the phone's 43, 60 and 70 dB
    read -41.4, -38.7 and -31.3 dBFS: quiet and "talking level" look the same, only a loud sound stands out.
- So the sound map is a **comparison between places**, in dBFS (louder than / not louder than), left mic only (`--mic left`, the default of `noise_map.py`;
  the logger's combined `noise_dB` would be flattened by the right mic's constant). Squares at or below the floor are drawn grey and labelled "quiet": the mic
  cannot tell how quiet. Do not save a calibration with `--force`: it would print wrong decibels.
- Left mic gain is **8 of 16 (50 %)** from now on (`amixer -c 0 sset Mic 8`; check with `calibrate_noise.py --check`). Do not change it between surveys. It can be
  lost when the mic is unplugged or the Dell restarts: set it again before a survey.
- What could still help: moving the mics away from the Dell and its fan, a better USB microphone, or a phone-meter reading at one loud place to say "about X dB here".

## Live position of the robot (any time, read-only)

`python -m display.server` (with `ROBOT_LINK_TOKEN` set and the tunnel up) also reads the robot's position from the Dell twice a second and draws it on the
operator page's floor plan, on the floor plan and, as a red dot, on the sound map: a blue marker with its heading, a trail of where it has been, and a line saying
x, y, heading, LOCALISED or not, and the navigation status. It works while a survey or teaching runs and needs no robot program; it only reads
(`GET /api/state`), so it cannot make the robot move. If it says it cannot read the base: the tunnel is down or the token is not set where the display
server was started.

## Positions: map frame, not /odom (found 2026-11-18 evening)

Until then the noise logger wrote positions from `/odom`, the wheel odometry, which starts where the base was switched on and is turned by however the robot
stood then (19 degrees on 2026-11-18). The sound squares came out turned and shifted against the floor plan (the readings at the survey points showed up
about 0.5 to 1.6 m away). The logger now reads the robot's pose from tf (`map` -> `base_footprint`) and writes **no row while the robot is not localised or the
pose is older than 3 s**. It needs the stack restarted to take effect. A log made before that can be corrected with the turn and shift fitted from where the survey
stopped (`python tools/noise_map.py run_data/noise_log.csv --odom-to-map 19.1 -0.09 -0.09 --until 450`; the log of the 16:08 survey), but fit a new one from the survey's
own visits (`data/noise/survey_log.json`) each time. A log from the fixed logger needs none of this.

## 1. Plug in the microphones (Dell stopped, or just at the hub)

- Left microphone: hub port **6** (as before, `1-2.6`).
- Right microphone: hub port **5** (`1-2.5`), not 7: the LiDAR is there.
- Tape both cables down like the LiDAR's: a dropped USB cable is what stopped the LiDAR on 2026-11-17.
- Check: `arecord -l` shows two cards named `UACDemoV1.0`.

## 2. Tell the robot which microphone is left and right (Dell, stack stopped)

```
cd ~/lbr-dell/turtlebot
MIC_CONFIG=$PWD/run_data/mic_config.json python3 setup_mics.py
```

You clap next to the left microphone, then the right, then make a steady sound in front of the robot. It writes
`run_data/mic_config.json` (by USB port). From then on `./run_robot_link.sh` says *Using the LEFT/RIGHT USB mics from …/run_data/mic_config.json*.

## 3. Calibrate (does NOT work with the current microphones, see 2026-11-18: skip it; the map is dBFS and relative)

Left microphone gain: `amixer -c 0 sset Mic 8` and `amixer -c 0 cset name='Auto Gain Control' off`, then `calibrate_noise.py --check` should say 50 %, AGC off for the left.
The calibration steps below are kept for better microphones:

1. Start the stack as usual: `./run_robot_link.sh --amcl --local`. The log must **not** say "No noise readings yet".
2. In a second Dell terminal, with the robot standing still and the room quiet:
   ```
   cd ~/lbr-dell/turtlebot
   MIC_CONFIG=$PWD/run_data/mic_config.json python3 calibrate_noise.py --check     # gain and automatic gain control: only looks
   MIC_CONFIG=$PWD/run_data/mic_config.json python3 calibrate_noise.py              # three levels: you make a steady sound, type what the phone shows
   ```
   It saves `~/noise_calibration.json` (the path the whole stack reads; it is new, not another person's file).
3. **Do not change the microphones' gain afterwards**, or the calibration is wrong.
4. **Restart the stack** (Ctrl-C, then start it again). The noise log is rewritten at every start and has the real-dB columns (`sound_dB …`) only
   when a calibration existed at the start.

## 4. Plan the survey (read-only, on the Jetson)

```
cd ~/lbr-dell-link
python tools/sound_survey.py plan                    # points 1 m apart, 0.35 m from walls and zones; writes data/noise/survey_plan.json
```

Open the operator page (`python -m display.server`, then http://localhost:8765/operator), **Sound map** panel (tick "Survey points and planned route"): the blue numbered dots are the
listening points in visiting order. `--spacing 1.5` makes fewer points, `--start X Y` plans from another start, `--clearance` changes the
distance from walls. On the 2026-11-17 map it gives 15 points, about 7 minutes.

## 5. DRIVES: the survey

Tunnel up (`scripts/robot_link_tunnel.sh robot@<Dell address> 8010`), the Dell stack running, `check` says LOCALISED, the robot on its parking
spot, no keyboard-teleop window open:

```
python tools/sound_survey.py run                      # asks: area clear AND someone at the power switch? type yes
python tools/sound_survey.py run --dwell 10 --max 6   # stand 10 s at each point; only the first 6 (a short try first)
```

It checks each leg on the map, drives at 0.2 m/s at most, stands still `--dwell` seconds at each point (the logger writes no reading while the
robot moves or speaks, so it listens only while still), then drives back to where it started. It stops on a bumper hit, after three failed
legs in a row, when the base is not localised or the link is lost, and on Ctrl-C. Every visit is logged to `data/noise/survey_log.json`.

Without driving by script: drive with the keyboard (as for teaching), stop at places for 8 to 10 s each, and cover the room evenly.

## 6. Make the sound map

**Copy the log before the stack is restarted** (it is overwritten at every start):

```
python tools/noise_map.py run_data/noise_log.csv --pull robot@<Dell address>   # fetches the Dell's log, writes data/noise/noise_grid.json (left mic only)
                                                                               # and says what the quietest tenth is: use it for --floor below
python tools/noise_map.py run_data/noise_log.csv --floor -39 --min-n 3 --png sound.png   # grey "quiet" at or below -39 dBFS (quietest tenth + about 2); picture too
```

Refresh the operator page: **Sound map** panel. Hover over the map to read the blended level at that spot.

**The Sound map does not update by itself:** it shows `data/noise/noise_grid.json`, which `noise_map.py` writes from a copy of the Dell's log. Run the two commands above after each survey (the page picks the new file up within 5 s, no restart). The log is overwritten whenever the stack starts, so copy it before restarting. Put several runs
together by listing several CSV files (keep copies with different names, for example `noise_morning.csv`, `noise_busy.csv`).

## If something is off

| What you see | What it means |
|---|---|
| "USB mic not found on port …" in `run_logs/noise_logger.log` | the microphones are not plugged in where `mic_config.json` says. Redo step 2 after plugging them in |
| The Sound map says **Not calibrated … dBFS** | expected with these microphones: the numbers only compare places (see 2026-11-18) |
| Every square the same / all grey | all places are at the microphone's floor, or `--floor` is too high. Check `left_dB` in the CSV: it should move when you clap near the left mic |
| No squares, "no readings with a position" | the logger writes nothing while the robot moves or speaks: let it stand still at each point |
| Few points in the plan | the map shows only ground it has seen; extend the map, or use `--spacing 1.5 --clearance 0.3` |
| The robot stops with NOT LOCALISED mid-survey | the LiDAR stopped (check the cable), see `docs/robot_link.md`; start the stack again |
