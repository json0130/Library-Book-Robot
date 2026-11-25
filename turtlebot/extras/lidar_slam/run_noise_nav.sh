#!/bin/bash
# PART 2 - Click-to-go noise map on a saved map. Starts everything in one window.
#
# Run:   bash ~/run_noise_nav.sh            (uses ~/lab_map.yaml)
#        bash ~/run_noise_nav.sh lab_map2   (uses ~/lab_map2.yaml)
#
# Put the robot on the spot where the map was started, facing the same way.
# On your PC run  ssh -L 8000:localhost:8000 robot@<robot ip>
# then open       http://localhost:8000   and click where the robot should go.
#
# Keyboard control is NOT started here, because while it runs it keeps sending
# "stop" and blocks navigation. To take over by hand, open a second window and
# run:  roslaunch turtlebot_teleop keyboard_teleop.launch   (Ctrl-C it to hand
# control back to the clicks). The web page also has a Stop button.
# Press Ctrl-C in this window to finish: the noise map is saved, then
# everything shuts down.

set -m
MAP_NAME=${1:-lab_map}
MAP_FILE=~/$MAP_NAME.yaml
LOG=~/run_logs; mkdir -p "$LOG"
PIDS=()

say()  { echo -e "\e[1;32m>>> $*\e[0m"; }
warn() { echo -e "\e[1;31m!!! $*\e[0m"; }

stop_all() {
    trap - INT TERM
    echo
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
for f in "$MAP_FILE" ~/noise_navigation.launch ~/hokuyo_costmap.yaml ~/scan_filter.py ~/noise_logger.py ~/noise_nav_web.py; do
    [ -f "$f" ] || { warn "Missing file: $f"; exit 1; }
done
if [ -e /dev/ttyACM0 ]; then
    say "Giving the LiDAR permission (may ask for the robot password)"
    sudo chmod a+rw /dev/ttyACM0
else
    warn "LiDAR not found at /dev/ttyACM0 - check it is plugged in"; exit 1
fi

# ---- 1. robot base ----
say "1/5 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
until rostopic list > /dev/null 2>&1; do sleep 1; done
if ! timeout 20 rostopic echo -n 1 /odom/header > /dev/null 2>&1; then
    warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"
    stop_all
fi
say "    robot base OK"

# ---- 2. LiDAR + map + AMCL + move_base ----
say "2/5 Starting navigation with map $MAP_FILE ..."
start navigation roslaunch ~/noise_navigation.launch map_file:="$MAP_FILE"
if ! timeout 20 rostopic echo -n 1 /hokuyo_scan/header > /dev/null 2>&1; then
    warn "No LiDAR data. See $LOG/navigation.log"; stop_all
fi
start scan_filter python3 ~/scan_filter.py
if ! timeout 10 rostopic echo -n 1 /hokuyo_scan_filtered/header > /dev/null 2>&1; then
    warn "Scan filter not working. See $LOG/scan_filter.log"; stop_all
fi
if ! timeout 30 rostopic echo -n 1 /move_base/status/header > /dev/null 2>&1; then
    warn "move_base did not start. See $LOG/navigation.log"; stop_all
fi
say "    navigation OK"

# ---- 3. noise logger ----
say "3/5 Starting noise logger ..."
start noise_logger python3 ~/noise_logger.py
if ! timeout 15 rostopic echo -n 1 /noise_level > /dev/null 2>&1; then
    warn "No noise readings - check the mic. See $LOG/noise_logger.log"
fi

# ---- 4. web page ----
say "4/5 Starting the click-to-go web page ..."
start noise_web python3 ~/noise_nav_web.py
sleep 3
say "    on your PC: ssh -L 8000:localhost:8000 robot@<robot-ip>"
say "    then open   http://localhost:8000"
say "    Check the blue dot is where the robot really is (or use 'Set robot position')."

# ---- 5. ready ----
say "5/5 Ready! Click white spots on the web page to drive the robot."
say "    Emergency stop: the red Stop button on the page."
say "    To drive by hand: in ANOTHER window run"
say "        roslaunch turtlebot_teleop keyboard_teleop.launch"
say "    and Ctrl-C it when done (clicks won't work while it is running)."
say "    Press Ctrl-C HERE to finish: saves the noise map and stops everything."
echo
while true; do
    read -r -t 5 _ || true          # wait 5 s (a built-in, so Ctrl-C reaches this script)
    if ! kill -0 "${PIDS[1]}" 2>/dev/null; then
        warn "Navigation stopped unexpectedly. See $LOG/navigation.log"; stop_all
    fi
done
