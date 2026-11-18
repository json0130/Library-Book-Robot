#!/bin/bash
# Makes the LiDAR map for ./run_robot_link.sh --amcl: robot base, Hokuyo LiDAR, scan filter, gmapping and the keyboard in this
# window. When you finish (Ctrl-C), map_saver writes maps/<name>.pgm and maps/<name>.yaml in THIS folder, plus a .png to look
# at. It never writes to the home folder, and never replaces a map unless asked (--overwrite keeps the old one as .bak).
#
# THE ROBOT DRIVES: you drive it with the keyboard. Only with the area clear and someone at the power switch.
#
# Tidy the room first: chairs pushed in under the desks, bags off the floor, and NOBODY in view of the LiDAR while it maps (stand
# behind the robot, out of its 240 degrees). What is on the map is what AMCL expects for ever: a chair left out is a "wall" it
# keeps looking for.
# Start with the robot ON ITS STATION, facing its usual way: that spot becomes (0, 0) facing +x on the new map. The station in
# ../data/floorplan.yaml (where AMCL starts) is then  station: {level: 1, x: 0.0, y: 0.0, yaw: 0.0}  until poses are taught again.
#
# Run (from this folder):
#     ./run_lidar_mapping.sh                 writes maps/lidar_map.*  (what run_robot_link.sh --amcl uses)
#     ./run_lidar_mapping.sh lab2            writes maps/lab2.*       (then: ./run_robot_link.sh --amcl --map maps/lab2.yaml)
#     ./run_lidar_mapping.sh --overwrite     replaces maps/lidar_map.* (the old files become lidar_map.bak-<time>.*)
#     ./run_lidar_mapping.sh --check         only look: is another ROS stack or script running? Starts nothing.
#     ./run_lidar_mapping.sh --dry-run       print what would be started, start nothing
#
# Driving for a good map: slowly (the keyboard starts at 0.2 m/s; keep it there or lower), all the way round the room, close the
# loop (come back past the start), stay about a metre from walls, and turn slowly. Glass and black surfaces show as gaps.

set -m
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0; DRY=0; OVERWRITE=0; NAME=lidar_map
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --dry-run) DRY=1 ;;
        --overwrite) OVERWRITE=1 ;;
        -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "unknown option $arg (see --help)"; exit 2 ;;
        *) NAME="$arg" ;;
    esac
done
if ! [[ "$NAME" =~ ^[A-Za-z0-9_-]{1,40}$ ]]; then echo "the map name may only have letters, digits, - and _ (got '$NAME')"; exit 2; fi
MAPS="$REPO/maps"; DATA="$REPO/run_data"; LOG="$REPO/run_logs"
PIDS=()
MAPPING=0                                   # 1 once gmapping is up: from then on, stopping saves the map first

say()  { echo -e "\e[1;32m>>> $*\e[0m"; }
warn() { echo -e "\e[1;31m!!! $*\e[0m"; }
bail() { if [ $DRY -eq 1 ]; then warn "(dry run: carrying on)"; else exit 1; fi; }

# rostopic can ignore the polite stop `timeout` sends, so it is killed outright 2 s later (-k)
wait_topic() {
    local limit=$1 topic=$2 end=$(( SECONDS + $1 ))
    while [ $SECONDS -lt $end ]; do
        timeout -k 2 4 rostopic echo -n 1 "$topic" > /dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

start() {
    local name=$1; shift
    if [ $DRY -eq 1 ]; then echo "    would start $name: $*"; return; fi
    "$@" > "$LOG/$name.log" 2>&1 &
    PIDS+=($!)
}

# map_saver into a temporary name, then into place: a failed save never costs the old map
save_map() {
    local tmp=".$NAME.saving" stamp
    say "Saving the map as $MAPS/$NAME ..."
    if ! (cd "$MAPS" && timeout -k 2 30 rosrun map_server map_saver -f "$tmp" > "$LOG/map_saver.log" 2>&1) \
            || [ ! -f "$MAPS/$tmp.pgm" ] || [ ! -f "$MAPS/$tmp.yaml" ]; then
        warn "Saving the map failed (see $LOG/map_saver.log). gmapping is stopped next, so this map is lost."
        rm -f "$MAPS/$tmp.pgm" "$MAPS/$tmp.yaml"
        return 1
    fi
    if [ -f "$MAPS/$NAME.yaml" ] || [ -f "$MAPS/$NAME.pgm" ]; then
        stamp=$(date +%Y%m%d-%H%M%S)
        for ext in pgm yaml png; do [ -f "$MAPS/$NAME.$ext" ] && mv "$MAPS/$NAME.$ext" "$MAPS/$NAME.bak-$stamp.$ext"; done
        sed -i "s|^image:.*|image: $NAME.bak-$stamp.pgm|" "$MAPS/$NAME.bak-$stamp.yaml" 2>/dev/null
        say "    the old map is kept as $MAPS/$NAME.bak-$stamp.*"
    fi
    mv "$MAPS/$tmp.pgm" "$MAPS/$NAME.pgm"
    sed "s|^image:.*|image: $NAME.pgm|" "$MAPS/$tmp.yaml" > "$MAPS/$NAME.yaml" && rm -f "$MAPS/$tmp.yaml"
    python3 - "$MAPS/$NAME.pgm" "$MAPS/$NAME.png" <<'EOF' 2>/dev/null && say "    a picture of it: $MAPS/$NAME.png"
import sys, numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
f = open(sys.argv[1], 'rb'); f.readline()
line = f.readline()
while line.startswith(b'#'):
    line = f.readline()
w, h = map(int, line.split()); f.readline()
plt.imsave(sys.argv[2], np.frombuffer(f.read(w * h), np.uint8).reshape(h, w), cmap='gray')
EOF
    say "Saved $MAPS/$NAME.pgm and $MAPS/$NAME.yaml"
    say "    Use it:  ./run_robot_link.sh --amcl$( [ "$NAME" != lidar_map ] && echo " --map maps/$NAME.yaml")"
    say "    The spot where mapping started is (0, 0) facing +x. If that was the station, set in ../data/floorplan.yaml:"
    say "        station: {level: 1, x: 0.0, y: 0.0, yaw: 0.0}"
    say "    For the visitor screen (on the Jetson, after copying the two files):  python tools/map_to_plan.py <the .yaml> lab"
}

finish() {
    trap - INT TERM
    echo
    [ $MAPPING -eq 1 ] && save_map
    if [ ${#PIDS[@]} -gt 0 ]; then
        say "Stopping everything this script started ..."
        for (( i=${#PIDS[@]}-1; i>=0; i-- )); do kill -INT "${PIDS[$i]}" 2>/dev/null; sleep 1; done
        wait 2>/dev/null
    fi
    say "All stopped. Logs are in $LOG"
    exit 0
}
trap finish INT TERM

# ---- 0. only one ROS stack at a time (as in run_robot_link.sh) ----
set --                                      # `source` would hand our arguments to catkin's setup.sh (see run_robot_link.sh)
command -v roslaunch > /dev/null || source /opt/ros/noetic/setup.bash
[ -f ~/turtlebot2/devel/setup.bash ] && source ~/turtlebot2/devel/setup.bash
export TURTLEBOT_BASE=kobuki TURTLEBOT_3D_SENSOR=astra
export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311} ROS_HOSTNAME=${ROS_HOSTNAME:-localhost}

others() {
    local pid
    for pid in $(pgrep -f "$1" 2>/dev/null); do
        [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ] || echo "$pid"
    done
}
# This script's own name is not in the list (its sub-shells would match it): a second copy is found by its pid file.
SCRIPTS='run_(vslam_noise_nav|vslam_mapping|noise_nav|mapping|robot_link)\.sh'
PATTERNS=(
    '(^|/)(roscore|rosmaster|roslaunch)( |$)'
    '(^|/)kobuki_node( |$)|kobuki_node/KobukiNodelet|__name:=mobile_base_nodelet_manager'
    "^([^ ]*/)?(ba)?sh( -[a-zA-Z]+)* ([^ ]*/)?$SCRIPTS|^([^ ]*/)?$SCRIPTS( |\$)"
)
conflicts=""
for pattern in "${PATTERNS[@]}"; do
    for pid in $(others "$pattern"); do
        conflicts+="$(ps -o pid=,user=,etime=,args= -p "$pid" 2>/dev/null | cut -c1-150)"$'\n'
    done
done
for pidfile in "$DATA/run_robot_link.pid" "$DATA/run_lidar_mapping.pid"; do
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        conflicts+="$(ps -o pid=,user=,etime=,args= -p "$(cat "$pidfile")" | cut -c1-150)"$'\n'
    fi
done
if [ -n "$conflicts" ] || rostopic list > /dev/null 2>&1; then
    warn "Another ROS stack is already running on this computer, so this script will NOT start (and will not stop it):"
    [ -n "$conflicts" ] && echo "$conflicts" | sed 's/^/      /'
    [ -z "$conflicts" ] && echo "      a ROS master answers (rostopic list works) but no known process was found"
    echo "    Stop it yourself (Ctrl-C in its window), then start this again."
    exit 1
fi
say "No other ROS stack is running."
[ $CHECK -eq 1 ] && exit 0
if [ $DRY -eq 0 ]; then
    mkdir -p "$DATA" "$LOG" "$MAPS"; echo $$ > "$DATA/run_lidar_mapping.pid"
    trap 'rm -f "$DATA/run_lidar_mapping.pid"' EXIT
fi

# ---- 1. checks ----
if [ -f "$MAPS/$NAME.yaml" ] && [ $OVERWRITE -eq 0 ]; then
    warn "$MAPS/$NAME.yaml exists already. Give another name (./run_lidar_mapping.sh lab2) or --overwrite (the old one is kept as .bak)."
    bail
fi
for f in "$REPO/config/lidar_mapping.launch" "$REPO/nodes/scan_filter.py"; do
    [ -f "$f" ] || { warn "Missing file: $f"; bail; }
done
for p in gmapping map_server turtlebot_teleop; do
    rospack find $p > /dev/null 2>&1 || { warn "ROS package $p is not installed"; bail; }
done
LIDAR=$(ls /dev/serial/by-id/*[Hh]okuyo* 2>/dev/null | head -1)
[ -z "$LIDAR" ] && LIDAR=$(ls /dev/ttyACM* 2>/dev/null | head -1)
[ -z "$LIDAR" ] && { warn "LiDAR not found - plug it in"; bail; }
LIDAR_DEV=$(readlink -f "${LIDAR:-/dev/null}")
if [ -n "$LIDAR" ] && { [ ! -r "$LIDAR_DEV" ] || [ ! -w "$LIDAR_DEV" ]; }; then
    warn "No permission to use the LiDAR ($LIDAR_DEV). This script does not use sudo. Either, once:"
    echo "        sudo usermod -aG dialout $(whoami)      # permanent; then log out and in again"
    echo "    or, until the next time it is plugged in:"
    echo "        sudo chmod a+rw $LIDAR_DEV"
    bail
fi
say "LiDAR found: $LIDAR -> $LIDAR_DEV"

# ---- 2. robot base ----
say "1/3 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
if [ $DRY -eq 0 ]; then
    until rostopic list > /dev/null 2>&1; do sleep 1; done
    if ! wait_topic 20 /odom/header > /dev/null 2>&1; then
        warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"; finish
    fi
fi
say "    robot base OK"

# ---- 3. LiDAR + scan filter + gmapping ----
say "2/3 Starting LiDAR + map making (gmapping) ..."
start mapping roslaunch "$REPO/config/lidar_mapping.launch" port:="$LIDAR_DEV"
start scan_filter python3 "$REPO/nodes/scan_filter.py"
if [ $DRY -eq 0 ]; then
    if ! wait_topic 60 /hokuyo_scan/header > /dev/null 2>&1; then warn "No LiDAR data. See $LOG/mapping.log"; finish; fi
    if ! wait_topic 20 /hokuyo_scan_filtered/header > /dev/null 2>&1; then warn "Scan filter not working. See $LOG/scan_filter.log"; finish; fi
    if ! wait_topic 60 /map/info > /dev/null 2>&1; then warn "gmapping is not making a map. See $LOG/mapping.log"; finish; fi
fi
MAPPING=1
say "    LiDAR + map OK"

# ---- 4. the keyboard (in this window) ----
say "3/3 Keyboard control: drive SLOWLY round the whole room and back past the start."
say "    The robot DRIVES. Emergency stop: the power switch. Press Ctrl-C when the map is done: it is saved, then everything stops."
echo
if [ $DRY -eq 1 ]; then
    echo "    would run the keyboard: roslaunch turtlebot_teleop keyboard_teleop.launch"
    echo "    would then save: map_saver -f $MAPS/$NAME"
    exit 0
fi
roslaunch turtlebot_teleop keyboard_teleop.launch
finish
