#!/bin/bash
# Starts the whole robot stack for the Jetson: base, LiDAR, localisation, move_base, scan filter, map cleaner, noise logger, the
# click-to-go page (port 8000) and robot_link_api (port 8010, the link to the Jetson).
# Two ways to find the robot on the map (README.md, "Two ways to localise"):
#     --amcl     the LiDAR on a LiDAR map (run_lidar_mapping.sh makes it) with AMCL; the camera is not started. The station pose
#                from ../data/floorplan.yaml is sent to AMCL at start-up, so start with the robot ON its station.
#     (default)  the camera with RTAB-Map on ~/vslam_map.db (run_vslam_mapping.sh makes it).
# Based on run_vslam_noise_nav.sh, but it reads the files in THIS folder (the repo copy), keeps its output in this folder, and
# works on a copy of the RTAB-Map map, so it never touches the files in the home folder that the other scripts use.
#
# Run (from this folder):
#     ./run_robot_link.sh --amcl           LiDAR + AMCL on maps/lidar_map.yaml
#     ./run_robot_link.sh --amcl --map maps/lab2.yaml    another LiDAR map (a map_saver .yaml)
#     ./run_robot_link.sh                  camera + RTAB-Map on ~/vslam_map.db (copied, never changed)
#     ./run_robot_link.sh vslam_map2       camera + RTAB-Map on ~/vslam_map2.db
#     ./run_robot_link.sh --check          only look: is another ROS stack or script running? Starts nothing.
#     ./run_robot_link.sh --dry-run        print what would be started, start nothing
#     ./run_robot_link.sh --local          robot_link_api listens on 127.0.0.1 only (_host:=127.0.0.1): reachable from the Jetson only through
#                                          the ssh tunnel (scripts/robot_link_tunnel.sh on the Jetson), not from the rest of the Wi-Fi
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
CHECK=0; DRY=0; LOCAL=0; AMCL=0; MAP_NAME=vslam_map; MAP_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --dry-run) DRY=1 ;;
        --local) LOCAL=1 ;;
        --amcl) AMCL=1 ;;
        --map) [ $# -ge 2 ] || { echo "--map needs a file (a map_saver .yaml)"; exit 2; }; MAP_FILE="$2"; shift ;;
        --map=*) MAP_FILE="${1#--map=}" ;;
        -h|--help) sed -n '2,29p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "unknown option $1 (see --help)"; exit 2 ;;
        *) MAP_NAME="$1" ;;
    esac
    shift
done
if [ -n "$MAP_FILE" ] && [ $AMCL -eq 0 ]; then
    echo "--map is the LiDAR map for --amcl. For RTAB-Map give the map's name instead: ./run_robot_link.sh vslam_map2"; exit 2
fi
[ $AMCL -eq 1 ] && MAP_FILE=$(readlink -m "${MAP_FILE:-$REPO/maps/lidar_map.yaml}")
FLOORPLAN=$(readlink -m "$REPO/../data/floorplan.yaml")      # its station: is the pose AMCL starts from
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

# wait_topic SECONDS TOPIC: true once TOPIC has delivered a message. `rostopic echo TOPIC/field` fails at once when the topic has not
# been advertised yet (a node still starting), so ask again every few seconds instead of once with a long timeout. rostopic can
# ignore the polite stop `timeout` sends (it hung for an hour once), so it is killed outright 2 s later (-k).
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

# ---- 0. only one ROS stack at a time ----
# a non-interactive shell (ssh, nohup) has not read ~/.bashrc
# `source` hands our own arguments on to the setup files, and catkin's setup.sh passes them to _setup_util.py, where `--local` means
# "forget everything else in the environment" (so /opt/ros/noetic vanished and roslaunch could not find turtlebot_bringup).
# We have read our options already: clear them before sourcing anything.
set --
command -v roslaunch > /dev/null || source /opt/ros/noetic/setup.bash
[ -f ~/turtlebot2/devel/setup.bash ] && source ~/turtlebot2/devel/setup.bash
# set outright, as ~/.bashrc does: sourcing the workspace sets TURTLEBOT_3D_SENSOR=asus_xtion_pro, which starts the wrong camera driver
export TURTLEBOT_BASE=kobuki TURTLEBOT_3D_SENSOR=astra
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
SCRIPTS='run_(vslam_noise_nav|vslam_mapping|noise_nav|mapping|lidar_mapping)\.sh'
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
if [ $AMCL -eq 1 ]; then
    say "Localisation: LiDAR + AMCL on $MAP_FILE"
    if [ ! -f "$MAP_FILE" ]; then
        warn "LiDAR map not found: $MAP_FILE - make one first with ./run_lidar_mapping.sh"; bail
    else
        IMAGE=$(sed -n 's/^image:[[:space:]]*//p' "$MAP_FILE" | head -1 | tr -d "\"'")
        case "$IMAGE" in /*) ;; *) IMAGE="$(dirname "$MAP_FILE")/$IMAGE" ;; esac
        [ -f "$IMAGE" ] || { warn "The map's picture is missing: $IMAGE (named in $MAP_FILE)"; bail; }
    fi
    [ -f "$FLOORPLAN" ] || { warn "No $FLOORPLAN: its station: is the pose AMCL starts from"; bail; }
    LAUNCH_FILES="amcl_nav.launch"; NODES="set_initial_pose"
    for p in amcl map_server; do rospack find $p > /dev/null 2>&1 || { warn "ROS package $p is not installed"; bail; }; done
else
    say "Localisation: camera + RTAB-Map (./run_robot_link.sh --amcl uses the LiDAR instead)"
    DB="$DATA/$MAP_NAME.db"; ORIGINAL=~/$MAP_NAME.db
    [ -f "$ORIGINAL" ] || { warn "Map not found: $ORIGINAL - make one first with run_vslam_mapping.sh"; bail; }
    LAUNCH_FILES="vslam.launch"; NODES=""
    if rospack find rtabmap_slam > /dev/null 2>&1; then RTAB_PKG=rtabmap_slam
    elif rospack find rtabmap_ros > /dev/null 2>&1; then RTAB_PKG=rtabmap_ros
    else warn "RTAB-Map is not installed"; bail; RTAB_PKG=rtabmap_slam; fi
fi
for f in "$REPO/config/$LAUNCH_FILES" "$REPO/config/hokuyo_costmap.yaml"; do
    [ -f "$f" ] || { warn "Missing file: $f"; bail; }
done
for n in scan_filter map_cleaner noise_logger noise_nav_web robot_link_api $NODES; do
    [ -f "$REPO/nodes/$n.py" ] || { warn "Missing file: $REPO/nodes/$n.py"; bail; }
done
python3 -c "import numpy, matplotlib" 2>/dev/null || { warn "python3 needs numpy and matplotlib (the click-to-go page)"; bail; }

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

if [ $DRY -eq 0 ] && [ $AMCL -eq 0 ]; then
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
    if ! wait_topic 20 /odom/header > /dev/null 2>&1; then
        warn "No /odom from the robot base. Is the base switched ON and its USB plugged in?"; stop_all
    fi
fi
say "    robot base OK"

# ---- 3. localisation (AMCL or RTAB-Map) + LiDAR + move_base ----
if [ $AMCL -eq 1 ]; then
    NAV=amcl_nav
    say "2/6 Starting LiDAR + AMCL localisation + navigation (map $MAP_FILE) ..."
    start amcl_nav roslaunch "$REPO/config/amcl_nav.launch" map_file:="$MAP_FILE" port:="$LIDAR_DEV"
else
    NAV=vslam
    say "2/6 Starting camera localisation + LiDAR + navigation (map $DB) ..."
    start vslam roslaunch "$REPO/config/vslam.launch" localization:=true navigation:=true \
          rtabmap_use_scan:=false grid_sensor:=1 rtabmap_pkg:=$RTAB_PKG database_path:="$DB" port:="$LIDAR_DEV"
fi
start scan_filter python3 "$REPO/nodes/scan_filter.py"
start map_cleaner python3 "$REPO/nodes/map_cleaner.py"
if [ $DRY -eq 0 ]; then
    if ! wait_topic 60 /hokuyo_scan/header > /dev/null 2>&1; then warn "No LiDAR data. See $LOG/$NAV.log"; stop_all; fi
    if [ $AMCL -eq 0 ]; then
        if ! wait_topic 60 /camera/rgb/camera_info/header > /dev/null 2>&1; then warn "No camera images. See $LOG/vslam.log"; stop_all; fi
        if ! wait_topic 120 /map/info > /dev/null 2>&1; then warn "RTAB-Map did not load the map. See $LOG/vslam.log"; stop_all; fi
    else
        if ! wait_topic 30 /map/info > /dev/null 2>&1; then warn "map_server did not load $MAP_FILE. See $LOG/amcl_nav.log"; stop_all; fi
    fi
    if ! wait_topic 120 /map_clean/info > /dev/null 2>&1; then warn "Map cleaner is not working. See $LOG/map_cleaner.log"; stop_all; fi
    if ! wait_topic 90 /move_base/status/header > /dev/null 2>&1; then warn "move_base did not start. See $LOG/$NAV.log"; stop_all; fi
fi
if [ $AMCL -eq 1 ]; then
    # where AMCL starts: the station. Not fatal: without it the robot is "not localised" until someone sets its position.
    if [ $DRY -eq 1 ]; then
        echo "    would run set_initial_pose: python3 $REPO/nodes/set_initial_pose.py --floorplan $FLOORPLAN"
    elif timeout -k 2 60 python3 "$REPO/nodes/set_initial_pose.py" --floorplan "$FLOORPLAN" > "$LOG/set_initial_pose.log" 2>&1; then
        say "    AMCL starts from the station: $(grep -m1 'sent' "$LOG/set_initial_pose.log" | sed 's/^set_initial_pose: //')"
    else
        warn "AMCL did not take the station pose (see $LOG/set_initial_pose.log). The base reports NOT localised until its"
        warn "position is set: 'Set robot position' on the click-to-go page, or put it on the station and run"
        warn "    python3 $REPO/nodes/set_initial_pose.py"
    fi
    say "    LiDAR + AMCL + navigation OK"
else
    say "    camera + LiDAR + navigation OK"
fi

# ---- 4. noise logger (its own files, not the ones in the home folder) ----
say "3/6 Starting noise logger ..."
start noise_logger python3 "$REPO/nodes/noise_logger.py" _file:="$DATA/noise_log.csv"
[ $DRY -eq 0 ] && { wait_topic 15 /noise_level > /dev/null 2>&1 || warn "No noise readings yet - see $LOG/noise_logger.log"; }

# ---- 5. click-to-go page ----
say "4/6 Starting the click-to-go web page ..."
WEB_ARGS=()
[ $AMCL -eq 1 ] && WEB_ARGS=(_localiser:=amcl)
start noise_web python3 "$REPO/nodes/noise_nav_web.py" _save_png:="$DATA/noise_nav_map.png" _save_csv:="$DATA/noise_nav_log.csv" "${WEB_ARGS[@]}"

# ---- 6. the link to the Jetson ----
say "5/6 Starting robot_link_api (the link to the Jetson) ..."
LINK_ARGS=()
if [ $LOCAL -eq 1 ]; then LINK_ARGS=(_host:=127.0.0.1); fi
[ $AMCL -eq 1 ] && LINK_ARGS+=(_localiser:=amcl)
ADDR=$(python3 "$REPO/nodes/robot_link_api.py" "${LINK_ARGS[@]}" --print-address)
start robot_link_api python3 "$REPO/nodes/robot_link_api.py" "${LINK_ARGS[@]}"
if [ $DRY -eq 0 ]; then
    sleep 3
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then warn "robot_link_api stopped. See $LOG/robot_link_api.log"; stop_all; fi
fi
say "6/6 Ready."
if [ $LOCAL -eq 1 ]; then
    say "    Listening on 127.0.0.1:8010 only. On the Jetson:  scripts/robot_link_tunnel.sh   and   turtlebot.base_url: http://127.0.0.1:8010"
    say "    Click-to-go page (from your PC):  ssh -L 8000:localhost:8000 $(whoami)@<the Dell's Wi-Fi address>   then  http://localhost:8000"
else
    say "    On the Jetson:  turtlebot.base_url: http://${ADDR:-<wifi address>}:8010   (config/robot.yaml)"
    say "    Click-to-go page (from your PC):  ssh -L 8000:localhost:8000 $(whoami)@${ADDR:-<wifi address>}   then  http://localhost:8000"
fi
say "    The robot drives when it is sent a goal. Emergency stop: the red Stop button on the page, or the power switch."
say "    Press Ctrl-C HERE to stop everything this script started."
[ $DRY -eq 1 ] && exit 0
echo
while true; do
    sleep 5
    if ! kill -0 "${PIDS[1]}" 2>/dev/null; then warn "Navigation stopped unexpectedly. See $LOG/$NAV.log"; stop_all; fi
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then warn "robot_link_api stopped. See $LOG/robot_link_api.log"; stop_all; fi
done
