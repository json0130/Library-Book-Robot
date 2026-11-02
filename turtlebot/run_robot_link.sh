#!/bin/bash
# Starts the whole robot stack for the Jetson: base, camera, LiDAR, RTAB-Map localisation, move_base, scan filter, map cleaner,
# noise logger, the click-to-go page (port 8000) and robot_link_api (port 8010, the link to the Jetson).
# Based on run_vslam_noise_nav.sh, but it reads the files in THIS folder (the repo copy), keeps its output in this folder, and
# works on a copy of the map, so it never touches the files in the home folder that the other scripts use.
#
# Run (from this folder):
#     ./run_robot_link.sh                  uses ~/vslam_map.db (copied, never changed)
#     ./run_robot_link.sh vslam_map2       uses ~/vslam_map2.db
#     ./run_robot_link.sh --check          only look: is another ROS stack or script running? Starts nothing.
#     ./run_robot_link.sh --dry-run        print what would be started, start nothing
#
# Needs: ROBOT_LINK_TOKEN (the shared secret, same as on the Jetson). Put it in .robot_link_env in this folder
#        (one line: export ROBOT_LINK_TOKEN=...; chmod 600 .robot_link_env) or export it first.
#
# Only one ROS stack can run at a time. If a roscore, the Kobuki driver or run_vslam_noise_nav.sh / run_vslam_mapping.sh is
# already running, this script says so and exits; it never stops anything it did not start itself.
#
# The base DRIVES when the Jetson (or the click-to-go page) sends a goal. Be next to the power switch.
# Press Ctrl-C in this window to stop everything this script started.

set -m
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0; DRY=0; MAP_NAME=vslam_map
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "unknown option $arg (see --help)"; exit 2 ;;
        *) MAP_NAME="$arg" ;;
    esac
done
DATA="$REPO/run_data"; LOG="$REPO/run_logs"
PIDS=()

say()  { echo -e "\e[1;32m>>> $*\e[0m"; }
warn() { echo -e "\e[1;31m!!! $*\e[0m"; }
# a problem that stops a real start; --dry-run reports it and carries on so the whole plan is shown
bail() { if [ $DRY -eq 1 ]; then warn "(dry run: carrying on)"; else exit 1; fi; }

stop_all() {
    trap - INT TERM
    echo
    if [ ${#PIDS[@]} -eq 0 ]; then say "Nothing was started - nothing to stop."; exit 0; fi
    say "Stopping everything this script started (the API cancels any goal first) ..."
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do kill -INT "${PIDS[$i]}" 2>/dev/null; sleep 1; done
    wait 2>/dev/null
    say "All stopped. Logs are in $LOG, data in $DATA"
    exit 0
}
trap stop_all INT TERM

start() {
    local name=$1; shift
    if [ $DRY -eq 1 ]; then echo "    would start $name: $*"; return; fi
    "$@" > "$LOG/$name.log" 2>&1 &
    PIDS+=($!)
}

# ---- 0. only one ROS stack at a time ----
# a non-interactive shell (ssh, nohup) has not read ~/.bashrc
command -v roslaunch > /dev/null || source /opt/ros/noetic/setup.bash
[ -f ~/turtlebot2/devel/setup.bash ] && source ~/turtlebot2/devel/setup.bash
export TURTLEBOT_BASE=${TURTLEBOT_BASE:-kobuki} TURTLEBOT_3D_SENSOR=${TURTLEBOT_3D_SENSOR:-astra}
export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311} ROS_HOSTNAME=${ROS_HOSTNAME:-localhost}   # ROS stays on this computer

# PIDs of processes whose command line matches a pattern, without this script and its parents.
others() {
    local pid
    for pid in $(pgrep -f "$1" 2>/dev/null); do
        [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ] || echo "$pid"
    done
}
# What counts as "another stack": a ROS master or roslaunch, the Kobuki driver, or one of the other run scripts. The patterns look at
# how the command STARTS, so an editor that merely has run_vslam_noise_nav.sh open does not count.
SCRIPTS='run_(vslam_noise_nav|vslam_mapping|noise_nav|mapping)\.sh'
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
# a second copy of this script (it cannot be found by name: its own sub-shells would match)
if [ -f "$DATA/run_robot_link.pid" ] && kill -0 "$(cat "$DATA/run_robot_link.pid")" 2>/dev/null; then
    conflicts+="$(ps -o pid=,user=,etime=,args= -p "$(cat "$DATA/run_robot_link.pid")" | cut -c1-150)"$'\n'
fi
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
    mkdir -p "$DATA" "$LOG"; echo $$ > "$DATA/run_robot_link.pid"
    trap 'rm -f "$DATA/run_robot_link.pid"' EXIT             # only after we wrote it: a refused start must not remove another copy's file
fi

# ---- 1. secret and files ----
if [ -f "$REPO/.robot_link_env" ]; then
    mode=$(stat -c %a "$REPO/.robot_link_env")
    if [ "${mode: -2}" != "00" ]; then
        warn "$REPO/.robot_link_env can be read by other users (mode $mode). Run:  chmod 600 $REPO/.robot_link_env"; exit 1
    fi
    source "$REPO/.robot_link_env"
fi
if [ -z "$ROBOT_LINK_TOKEN" ]; then
    warn "ROBOT_LINK_TOKEN is not set. Put  export ROBOT_LINK_TOKEN=<secret>  in $REPO/.robot_link_env (chmod 600) or export it."
    exit 1
fi
DB="$DATA/$MAP_NAME.db"; ORIGINAL=~/$MAP_NAME.db
[ -f "$ORIGINAL" ] || { warn "Map not found: $ORIGINAL - make one first with run_vslam_mapping.sh"; bail; }
for f in "$REPO"/config/{vslam.launch,hokuyo_costmap.yaml} "$REPO"/nodes/{scan_filter,map_cleaner,noise_logger,noise_nav_web,robot_link_api}.py; do
    [ -f "$f" ] || { warn "Missing file: $f"; bail; }
done
python3 -c "import numpy, matplotlib" 2>/dev/null || { warn "python3 needs numpy and matplotlib (the click-to-go page)"; bail; }
if rospack find rtabmap_slam > /dev/null 2>&1; then RTAB_PKG=rtabmap_slam
elif rospack find rtabmap_ros > /dev/null 2>&1; then RTAB_PKG=rtabmap_ros
else warn "RTAB-Map is not installed"; bail; RTAB_PKG=rtabmap_slam; fi

# the LiDAR, found by its serial id so it works in any USB port
LIDAR=$(ls /dev/serial/by-id/*[Hh]okuyo* 2>/dev/null | head -1)
[ -z "$LIDAR" ] && LIDAR=$(ls /dev/ttyACM* 2>/dev/null | head -1)
[ -z "$LIDAR" ] && { warn "LiDAR not found - plug it in (it is needed for obstacle avoidance)"; bail; }
LIDAR_DEV=$(readlink -f "${LIDAR:-/dev/null}")
if [ -n "$LIDAR" ] && { [ ! -r "$LIDAR_DEV" ] || [ ! -w "$LIDAR_DEV" ]; }; then
    warn "No permission to use the LiDAR ($LIDAR_DEV). This script does not use sudo. Either, once:"
    echo "        sudo usermod -aG dialout $(whoami)      # permanent; then log out and in again"
    echo "    or, until the next time it is plugged in:"
    echo "        sudo chmod a+rw $LIDAR_DEV"
    bail
fi
say "LiDAR found: $LIDAR -> $LIDAR_DEV"
[ -f ~/mic_config.json ] && say "Using the LEFT/RIGHT USB mics from ~/mic_config.json" \
    || warn "No ~/mic_config.json - noise will use the camera mic. Run: python3 $REPO/setup_mics.py"
if [ -f ~/noise_calibration.json ]; then say "Noise calibration found: the Jetson gets sound levels in dB"
else warn "No ~/noise_calibration.json: noise is reported in dBFS and the Jetson ignores it. Run: python3 $REPO/calibrate_noise.py"; fi

if [ $DRY -eq 0 ]; then
    mkdir -p "$LOG" "$DATA"
    # RTAB-Map opens its database for writing, so it gets a copy; the original in the home folder is never touched
    if [ ! -f "$DB" ] || [ "$ORIGINAL" -nt "$DB" ]; then
        say "Copying the map $ORIGINAL -> $DB ($(du -h "$ORIGINAL" | cut -f1)) ..."
        cp "$ORIGINAL" "$DB.part" && mv "$DB.part" "$DB"
    fi
fi

# ---- 2. robot base ----
say "1/6 Starting robot base (minimal.launch) ..."
start minimal roslaunch turtlebot_bringup minimal.launch
if [ $DRY -eq 0 ]; then
    until rostopic list > /dev/null 2>&1; do sleep 1; done
    if ! timeout 20 rostopic echo -n 1 /odom/header > /dev/null 2>&1; then
        warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"; stop_all
    fi
fi
say "    robot base OK"

# ---- 3. camera + RTAB-Map localisation + LiDAR + move_base ----
say "2/6 Starting camera localisation + LiDAR + navigation (map $DB) ..."
start vslam roslaunch "$REPO/config/vslam.launch" localization:=true navigation:=true \
      rtabmap_use_scan:=false grid_sensor:=1 rtabmap_pkg:=$RTAB_PKG database_path:="$DB" port:="$LIDAR_DEV"
start scan_filter python3 "$REPO/nodes/scan_filter.py"
start map_cleaner python3 "$REPO/nodes/map_cleaner.py"
if [ $DRY -eq 0 ]; then
    if ! timeout 20 rostopic echo -n 1 /hokuyo_scan/header > /dev/null 2>&1; then warn "No LiDAR data. See $LOG/vslam.log"; stop_all; fi
    if ! timeout 30 rostopic echo -n 1 /camera/rgb/camera_info/header > /dev/null 2>&1; then warn "No camera images. See $LOG/vslam.log"; stop_all; fi
    if ! timeout 60 rostopic echo -n 1 /map/info > /dev/null 2>&1; then warn "RTAB-Map did not load the map. See $LOG/vslam.log"; stop_all; fi
    if ! timeout 30 rostopic echo -n 1 /map_clean/info > /dev/null 2>&1; then warn "Map cleaner is not working. See $LOG/map_cleaner.log"; stop_all; fi
    if ! timeout 30 rostopic echo -n 1 /move_base/status/header > /dev/null 2>&1; then warn "move_base did not start. See $LOG/vslam.log"; stop_all; fi
fi
say "    camera + LiDAR + navigation OK"

# ---- 4. noise logger (its own files, not the ones in the home folder) ----
say "3/6 Starting noise logger ..."
start noise_logger python3 "$REPO/nodes/noise_logger.py" _file:="$DATA/noise_log.csv"
[ $DRY -eq 0 ] && { timeout 15 rostopic echo -n 1 /noise_level > /dev/null 2>&1 || warn "No noise readings yet - see $LOG/noise_logger.log"; }

# ---- 5. click-to-go page ----
say "4/6 Starting the click-to-go web page ..."
start noise_web python3 "$REPO/nodes/noise_nav_web.py" _save_png:="$DATA/noise_nav_map.png" _save_csv:="$DATA/noise_nav_log.csv"

# ---- 6. the link to the Jetson ----
say "5/6 Starting robot_link_api (the link to the Jetson) ..."
ADDR=$(python3 "$REPO/nodes/robot_link_api.py" --print-address)
start robot_link_api python3 "$REPO/nodes/robot_link_api.py"
if [ $DRY -eq 0 ]; then
    sleep 3
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then warn "robot_link_api stopped. See $LOG/robot_link_api.log"; stop_all; fi
fi
say "6/6 Ready."
say "    On the Jetson:  turtlebot.base_url: http://${ADDR:-<wifi address>}:8010   (config/robot.yaml)"
say "    Click-to-go page (from your PC):  ssh -L 8000:localhost:8000 $(whoami)@${ADDR:-<wifi address>}   then  http://localhost:8000"
say "    The robot drives when it is sent a goal. Emergency stop: the red Stop button on the page, or the power switch."
say "    Press Ctrl-C HERE to stop everything this script started."
[ $DRY -eq 1 ] && exit 0
echo
while true; do
    sleep 5
    if ! kill -0 "${PIDS[1]}" 2>/dev/null; then warn "Navigation stopped unexpectedly. See $LOG/vslam.log"; stop_all; fi
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then warn "robot_link_api stopped. See $LOG/robot_link_api.log"; stop_all; fi
done
