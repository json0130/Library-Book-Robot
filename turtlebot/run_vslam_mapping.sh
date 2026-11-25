#!/bin/bash
# PART 1 (visual SLAM) - Make a map with the CAMERA, driving with the keyboard.
# Starts everything in one window. The LiDAR is not needed for this step.
#
# Run (from the repo folder):
#        ./run_vslam_mapping.sh             (saves ~/vslam_map.db + ~/vslam_map.png)
#        ./run_vslam_mapping.sh vslam_map2  (saves with a different name)
#
# Drive SLOWLY with the keyboard in this window - especially turning.
# Press Ctrl-C when the map is done: the map is saved, then everything stops.
#
# Watch on your PC:  ssh -L 8080:localhost:8080 robot@<robot ip>
#   map:    http://localhost:8080/stream_viewer?topic=/map_image
#   camera: http://localhost:8080/stream_viewer?topic=/camera/rgb/image_raw

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
    if [ ${#PIDS[@]} -eq 0 ]; then
        say "Cancelled before starting - nothing was changed."
        exit 0
    fi
    if rostopic list 2>/dev/null | grep -q '^/map$'; then
        say "Saving 2D map as ~/$MAP_NAME ..."
        (cd ~ && timeout 30 rosrun map_server map_saver -f "$MAP_NAME" > "$LOG/map_saver.log" 2>&1) \
            && say "Saved ~/$MAP_NAME.pgm and ~/$MAP_NAME.yaml" || warn "2D map save failed (see $LOG/map_saver.log)"
        python3 - "$HOME/$MAP_NAME" <<'EOF' 2>/dev/null && say "Saved ~/$MAP_NAME.png"
import sys, numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
f = open(sys.argv[1] + '.pgm', 'rb'); f.readline(); f.readline()
w, h = map(int, f.readline().split()); f.readline()
plt.imsave(sys.argv[1] + '.png', np.frombuffer(f.read(), np.uint8).reshape(h, w), cmap='gray')
EOF
    fi
    say "Shutting down (RTAB-Map is saving $DB - this can take a few seconds) ..."
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do kill -INT "${PIDS[$i]}" 2>/dev/null; sleep 1; done
    wait 2>/dev/null
    [ -f "$DB" ] && say "Visual SLAM map saved: $DB ($(du -h "$DB" | cut -f1))"
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
for f in "$REPO/config/vslam.launch" "$REPO/nodes/map_viewer.py"; do
    [ -f "$f" ] || { warn "Missing file: $f"; exit 1; }
done
if rospack find rtabmap_slam > /dev/null 2>&1; then
    RTAB_PKG=rtabmap_slam
elif rospack find rtabmap_ros > /dev/null 2>&1; then
    RTAB_PKG=rtabmap_ros
else
    warn "RTAB-Map is not installed. Install it with:"
    warn "    sudo apt update && sudo apt install ros-noetic-rtabmap-ros"
    exit 1
fi
say "Using RTAB-Map package: $RTAB_PKG"
[ -f "$DB" ] && warn "$DB already exists - it will be REPLACED. Ctrl-C now to keep it (use another name)." && sleep 5

# ---- 1. robot base ----
say "1/5 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
until rostopic list > /dev/null 2>&1; do sleep 1; done
if ! timeout 20 rostopic echo -n 1 /odom/header > /dev/null 2>&1; then
    warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"
    stop_all
fi
say "    robot base OK"

# ---- 2. camera + RTAB-Map ----
say "2/5 Starting camera + visual SLAM (RTAB-Map) ..."
start vslam roslaunch "$REPO/config/vslam.launch" use_lidar:=false rtabmap_pkg:=$RTAB_PKG database_path:="$DB"
if ! timeout 30 rostopic echo -n 1 /camera/rgb/camera_info/header > /dev/null 2>&1; then
    warn "No camera images. Is the camera plugged in? See $LOG/vslam.log"; stop_all
fi
if ! timeout 20 rostopic echo -n 1 /camera/depth_registered/image_raw/header > /dev/null 2>&1; then
    warn "No depth images on /camera/depth_registered/image_raw. See $LOG/vslam.log"; stop_all
fi
if ! timeout 15 rosrun tf tf_echo base_footprint camera_rgb_optical_frame 2>/dev/null | grep -q Translation; then
    warn "The robot doesn't know where the camera is mounted (no TF base_footprint -> camera_rgb_optical_frame)."
    warn "Check TURTLEBOT_3D_SENSOR is set for your camera (currently: '$TURTLEBOT_3D_SENSOR') - see README"
    stop_all
fi
say "    camera OK"
if ! timeout 60 rostopic echo -n 1 /map/info > /dev/null 2>&1; then
    warn "RTAB-Map is not making a map. See $LOG/vslam.log"; stop_all
fi
say "    visual SLAM OK"

# ---- 3. map viewer + video server ----
say "3/5 Starting map viewer + video server ..."
start map_viewer python3 "$REPO/nodes/map_viewer.py"
start video rosrun web_video_server web_video_server
sleep 3
say "    On your PC: ssh -L 8080:localhost:8080 $ROBOT_USER@$ROBOT_IP"
say "    Map:    http://localhost:8080/stream_viewer?topic=/map_image"
say "    Camera: http://localhost:8080/stream_viewer?topic=/camera/rgb/image_raw"

# ---- 4. tips ----
say "4/5 Tips for a good camera map:"
say "    - drive SLOWLY and turn SLOWLY (fast turns blur the camera and it gets lost)"
say "    - point the camera at furniture/posters, not blank walls"
say "    - at the end, drive back past where you started (it fixes drift when it recognises a place)"

# ---- 5. keyboard control (in this window) ----
say "5/5 Keyboard control - drive around the room."
say "    Press Ctrl-C when the map is done: it saves the map and stops everything."
echo
roslaunch turtlebot_teleop keyboard_teleop.launch
stop_all
