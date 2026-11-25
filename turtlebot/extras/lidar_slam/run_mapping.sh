#!/bin/bash
# PART 1 - Make a map with the keyboard. Starts everything in one window.
#
# Run:   bash ~/run_mapping.sh            (saves the map as ~/lab_map)
#        bash ~/run_mapping.sh lab_map2   (saves it with a different name)
#
# Drive with the keyboard in this window. Press Ctrl-C when the map is done:
# the map is saved automatically, then everything shuts down.
# Watch the map: on your PC run  ssh -L 8080:localhost:8080 robot@<robot ip>
#                then open       http://localhost:8080/stream_viewer?topic=/map_image

set -m                                   # lets us stop each part cleanly
MAP_NAME=${1:-lab_map}
LOG=~/run_logs; mkdir -p "$LOG"
PIDS=()

say()  { echo -e "\e[1;32m>>> $*\e[0m"; }
warn() { echo -e "\e[1;31m!!! $*\e[0m"; }

stop_all() {
    trap - INT TERM
    echo
    if rostopic list 2>/dev/null | grep -q '^/map$'; then
        say "Saving map as ~/$MAP_NAME ..."
        (cd ~ && rosrun map_server map_saver -f "$MAP_NAME" > "$LOG/map_saver.log" 2>&1) \
            && say "Saved ~/$MAP_NAME.pgm and ~/$MAP_NAME.yaml" || warn "Map save failed (see $LOG/map_saver.log)"
        python3 - "$HOME/$MAP_NAME" <<'EOF' 2>/dev/null && say "Saved ~/$MAP_NAME.png"
import sys, numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
f = open(sys.argv[1] + '.pgm', 'rb'); f.readline(); f.readline()
w, h = map(int, f.readline().split()); f.readline()
plt.imsave(sys.argv[1] + '.png', np.frombuffer(f.read(), np.uint8).reshape(h, w), cmap='gray')
EOF
    fi
    say "Shutting everything down ..."
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do kill -INT "${PIDS[$i]}" 2>/dev/null; sleep 1; done
    wait 2>/dev/null
    say "All stopped. Logs are in $LOG"
    exit 0
}
trap stop_all INT TERM

start() {   # start <name> <command...>
    local name=$1; shift
    "$@" > "$LOG/$name.log" 2>&1 &
    PIDS+=($!)
}

# ---- 0. checks ----
if rostopic list > /dev/null 2>&1; then
    warn "ROS is already running. Press Ctrl-C in your other robot windows first, then try again."
    exit 1
fi
[ -f ~/lidar_mapping.launch ] || { warn "~/lidar_mapping.launch is missing"; exit 1; }
[ -f ~/map_viewer.py ]        || { warn "~/map_viewer.py is missing"; exit 1; }
if [ -e /dev/ttyACM0 ]; then
    say "Giving the LiDAR permission (may ask for the robot password)"
    sudo chmod a+rw /dev/ttyACM0
else
    warn "LiDAR not found at /dev/ttyACM0 - check it is plugged in"; exit 1
fi

# ---- 1. robot base ----
say "1/4 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
until rostopic list > /dev/null 2>&1; do sleep 1; done
if ! timeout 20 rostopic echo -n 1 /odom/header > /dev/null 2>&1; then
    warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"
    stop_all
fi
say "    robot base OK"

# ---- 2. LiDAR + gmapping ----
say "2/4 Starting LiDAR + map making ..."
start mapping roslaunch ~/lidar_mapping.launch
if ! timeout 20 rostopic echo -n 1 /hokuyo_scan/header > /dev/null 2>&1; then
    warn "No LiDAR data. See $LOG/mapping.log"; stop_all
fi
if ! timeout 30 rostopic echo -n 1 /map/info > /dev/null 2>&1; then
    warn "gmapping is not making a map. See $LOG/mapping.log"; stop_all
fi
say "    LiDAR + map OK"

# ---- 3. map viewer + video server ----
say "3/4 Starting map viewer + video server ..."
start map_viewer python3 ~/map_viewer.py
start video rosrun web_video_server web_video_server
sleep 3
say "    open http://localhost:8080/stream_viewer?topic=/map_image on your PC"
say "    (with the tunnel: ssh -L 8080:localhost:8080 robot@<robot-ip>)"

# ---- 4. keyboard control (in this window) ----
say "4/4 Keyboard control - drive slowly around the whole room."
say "    Press Ctrl-C when the map is done: it saves the map and stops everything."
echo
roslaunch turtlebot_teleop keyboard_teleop.launch
stop_all
