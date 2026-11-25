#!/bin/bash
# PART 2 (visual SLAM) - Click-to-go noise map on the camera-made map.
#   CAMERA (RTAB-Map localization) works out where the robot is on the map
#   LIDAR  avoids obstacles while driving (move_base)
#   MIC    measures noise, painted on the map
# Starts everything in one window.
#
# Run (from the repo folder):
#        ./run_vslam_noise_nav.sh             (uses ~/vslam_map.db)
#        ./run_vslam_noise_nav.sh vslam_map2  (uses ~/vslam_map2.db)
#
# Put the robot where you STARTED making the map, facing the same way.
# On your PC:  ssh -L 8000:localhost:8000 -L 8080:localhost:8080 robot@<robot ip>
# then open    http://localhost:8000   and click where the robot should go.
#
# Keyboard control is NOT started here (it would block the clicks). To drive by
# hand, open another window and run
#     roslaunch turtlebot_teleop keyboard_teleop.launch
# and Ctrl-C it when done. The web page has a red Stop button.
# Press Ctrl-C in this window to finish: saves the noise map, stops everything.

set -m
# Folder this script lives in (the cloned repo) - all other files are found from here
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROBOT_IP=$(hostname -I 2>/dev/null | awk '{print $1}'); ROBOT_IP=${ROBOT_IP:-<robot-ip>}
ROBOT_USER=$(whoami)
MAP_NAME=${1:-vslam_map}
DB=~/$MAP_NAME.db
LOG=~/run_logs; mkdir -p "$LOG"
PIDS=()

say()  { echo -e "\e[1;32m>>> $*\e[0m"; }
warn() { echo -e "\e[1;31m!!! $*\e[0m"; }

stop_all() {
    trap - INT TERM
    echo
    if [ ${#PIDS[@]} -eq 0 ]; then say "Cancelled before starting - nothing was changed."; exit 0; fi
    say "Saving noise map and shutting everything down ..."
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do kill -INT "${PIDS[$i]}" 2>/dev/null; sleep 1; done
    wait 2>/dev/null
    [ -f ~/noise_nav_map.png ] && say "Saved ~/noise_nav_map.png and ~/noise_nav_log.csv"
    say "All stopped. Logs are in $LOG"
    exit 0
}
trap stop_all INT TERM

start() {
    local name=$1; shift
    "$@" > "$LOG/$name.log" 2>&1 &
    PIDS+=($!)
}

# ---- 0. checks ----
if rostopic list > /dev/null 2>&1; then
    warn "ROS is already running. Press Ctrl-C in your other robot windows first, then try again."
    exit 1
fi
[ -f "$DB" ] || { warn "Map not found: $DB  - make one first with run_vslam_mapping.sh"; exit 1; }
for f in "$REPO"/config/{vslam.launch,hokuyo_costmap.yaml} "$REPO"/nodes/{scan_filter,map_cleaner,noise_logger,noise_nav_web}.py; do
    [ -f "$f" ] || { warn "Missing file: $f"; exit 1; }
done
if rospack find rtabmap_slam > /dev/null 2>&1; then RTAB_PKG=rtabmap_slam
elif rospack find rtabmap_ros > /dev/null 2>&1; then RTAB_PKG=rtabmap_ros
else warn "RTAB-Map is not installed"; exit 1; fi
# Find the LiDAR by its serial ID, so it works whichever USB port it is in
LIDAR=$(ls /dev/serial/by-id/*[Hh]okuyo* 2>/dev/null | head -1)
[ -z "$LIDAR" ] && LIDAR=$(ls /dev/ttyACM* 2>/dev/null | head -1)
if [ -n "$LIDAR" ]; then
    say "LiDAR found: $LIDAR -> $(readlink -f "$LIDAR")"
    say "Giving the LiDAR permission (may ask for the robot password)"
    sudo chmod a+rw "$(readlink -f "$LIDAR")"
else
    warn "LiDAR not found - plug it in (it is needed for obstacle avoidance)"; exit 1
fi
if [ -f ~/mic_config.json ]; then
    say "Using the LEFT/RIGHT USB mics from ~/mic_config.json"
else
    warn "No ~/mic_config.json - noise will use the camera mic. Run: python3 $REPO/setup_mics.py"
fi
# keep a copy of the previous noise map so a new run does not overwrite it
[ -f ~/noise_nav_map.png ] && cp ~/noise_nav_map.png ~/noise_nav_map_prev.png && cp ~/noise_nav_log.csv ~/noise_nav_log_prev.csv 2>/dev/null

# ---- 1. robot base ----
say "1/6 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
until rostopic list > /dev/null 2>&1; do sleep 1; done
if ! timeout 20 rostopic echo -n 1 /odom/header > /dev/null 2>&1; then
    warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"; stop_all
fi
say "    robot base OK"

# ---- 2. camera + RTAB-Map localization + LiDAR + move_base ----
say "2/6 Starting camera localization + LiDAR + navigation (map $DB) ..."
start vslam roslaunch "$REPO/config/vslam.launch" localization:=true navigation:=true \
      rtabmap_use_scan:=false grid_sensor:=1 rtabmap_pkg:=$RTAB_PKG database_path:="$DB" \
      port:="$LIDAR"
start scan_filter python3 "$REPO/nodes/scan_filter.py"
start map_cleaner python3 "$REPO/nodes/map_cleaner.py"
if ! timeout 20 rostopic echo -n 1 /hokuyo_scan/header > /dev/null 2>&1; then
    warn "No LiDAR data. See $LOG/vslam.log"; stop_all
fi
if ! timeout 30 rostopic echo -n 1 /camera/rgb/camera_info/header > /dev/null 2>&1; then
    warn "No camera images. See $LOG/vslam.log"; stop_all
fi
if ! timeout 60 rostopic echo -n 1 /map/info > /dev/null 2>&1; then
    warn "RTAB-Map did not load the map. See $LOG/vslam.log"; stop_all
fi
if ! timeout 30 rostopic echo -n 1 /map_clean/info > /dev/null 2>&1; then
    warn "Map cleaner is not working. See $LOG/map_cleaner.log"; stop_all
fi
say "    $(grep -o 'removed [0-9]* small black specks' "$LOG/map_cleaner.log" | tail -1) from the map"
if ! timeout 30 rostopic echo -n 1 /move_base/status/header > /dev/null 2>&1; then
    warn "move_base did not start. See $LOG/vslam.log"; stop_all
fi
say "    camera + LiDAR + navigation OK"

# ---- 3. noise logger ----
say "3/6 Starting noise logger ..."
start noise_logger python3 "$REPO/nodes/noise_logger.py" _file:="$HOME/noise_log.csv"
timeout 15 rostopic echo -n 1 /noise_level > /dev/null 2>&1 || warn "No noise readings yet - see $LOG/noise_logger.log"

# ---- 4. web page + video server ----
say "4/6 Starting the click-to-go web page + camera stream ..."
start noise_web python3 "$REPO/nodes/noise_nav_web.py"
start video rosrun web_video_server web_video_server
sleep 3

# ---- 5. how to connect ----
say "5/6 On your PC run:  ssh -L 8000:localhost:8000 -L 8080:localhost:8080 $ROBOT_USER@$ROBOT_IP"
say "    Click-to-go page: http://localhost:8000"
say "    Camera view:      http://localhost:8080/stream_viewer?topic=/camera/rgb/image_raw"

# ---- 6. ready ----
say "6/6 Wait for '✓ Camera knows where I am' on the page before clicking."
say "    If it stays red: turn the robot slowly on the spot (teleop in another window)"
say "    so the camera sees an area it mapped, or use 'Set robot position'."
say "    Emergency stop: the red Stop button on the page."
say "    Press Ctrl-C HERE to finish: saves the noise map and stops everything."
echo
while true; do
    read -r -t 5 _ || true
    if ! kill -0 "${PIDS[1]}" 2>/dev/null; then
        warn "Navigation stopped unexpectedly. See $LOG/vslam.log"; stop_all
    fi
done
