#!/usr/bin/env bash
# The whole demo in one command: the AI, the display server (face page + operator page) and the robot, in that order.
#
#   scripts/start_demo.sh                     local AI, display, robot with webcam, faces, mic, speaker and LLM (`sim --live`)
#   scripts/start_demo.sh --remote            the AI server (ai_server.host, 100.110.91.42) instead of the AI on this Jetson
#   scripts/start_demo.sh --base fake         the fake TurtleBot: drives move a dot on the floor plan, nothing moves
#   scripts/start_demo.sh --base turtlebot    the real TurtleBot: also keeps the ssh tunnel to the Dell up. Start the Dell's own
#                                             stack there yourself first (ssh dell; ~/lbr-dell/turtlebot/run_robot_link.sh --amcl),
#                                             only when the area is clear: the robot WILL drive
#   scripts/start_demo.sh --face              also open the face page full screen on this computer's screen (Firefox kiosk)
#   scripts/start_demo.sh --stop-ai           stop the local AI when the robot quits (by default it stays up: it takes ~20 s to start)
#
# Anything else goes to `python -m robot sim` (e.g. --start to run at once, --zone quiet, --no-camera).
# The robot starts STOPPED: press Start on the operator page (or type `system start` here). `quit` or Ctrl-C ends everything
# this script started; the local AI stays up unless --stop-ai (scripts/start_local_ai.sh stop).
set -uo pipefail

cd "$(dirname "$0")/.."
LOGS="run_data/logs"
mkdir -p "$LOGS"

BASE="" FACE=0 STOP_AI=0 SIM_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote) export AI_MODE=remote ;;
        --local) export AI_MODE=local ;;
        --base) BASE="${2:?--base needs fake or turtlebot}"; shift ;;
        --face) FACE=1 ;;
        --stop-ai) STOP_AI=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) SIM_ARGS+=("$1") ;;
    esac
    shift
done

say() { printf '\n== %s\n' "$*"; }
listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

read -r MODE AI_HOST AI_PORT DISPLAY_PORT < <(python3 -c "
from robot.config import load_config
c = load_config()
print(c.ai_mode, c.ai_host, c.ai_port, c.display['port'])") || { echo "ERROR: cannot read config/robot.yaml" >&2; exit 1; }

STARTED=()                                   # pids this script started, stopped at the end
cleanup() {
    for pid in "${STARTED[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
    if [[ $STOP_AI == 1 && $MODE == local ]]; then scripts/start_local_ai.sh stop; fi
    echo "demo stopped (the display server and tunnel this script started are down)"
}
trap cleanup EXIT

# ---- 1. the AI
if [[ $MODE == local ]]; then
    say "1/3  the local AI on this Jetson (127.0.0.1:$AI_PORT)"
    scripts/start_local_ai.sh start || { echo "ERROR: the local AI did not start (try --remote)" >&2; exit 1; }
else
    say "1/3  the AI server at $AI_HOST:$AI_PORT"
    python3 -m robot check || echo "WARNING: the AI server does not answer: no LLM, speech or server faces (faces fall back to OpenCV)"
fi

# ---- 2. the display server
say "2/3  the display server on port $DISPLAY_PORT"
if listening "$DISPLAY_PORT"; then
    echo "already running on port $DISPLAY_PORT: using it"
else
    python3 -m display.server --port "$DISPLAY_PORT" >"$LOGS/display.log" 2>&1 &
    STARTED+=($!)
    for _ in $(seq 1 50); do curl -fs -o /dev/null "http://127.0.0.1:$DISPLAY_PORT/" && break; sleep 0.2; done
    curl -fs -o /dev/null "http://127.0.0.1:$DISPLAY_PORT/" || { echo "ERROR: the display server did not start; see $LOGS/display.log" >&2; exit 1; }
    echo "running (log: $LOGS/display.log)"
fi

# ---- the TurtleBot link (only with --base turtlebot)
if [[ $BASE == turtlebot ]]; then
    say "     the ssh tunnel to the Dell (ssh dell, over Tailscale)"
    [[ -n "${ROBOT_LINK_TOKEN:-}" ]] || echo "WARNING: ROBOT_LINK_TOKEN is not set: the robot cannot talk to the Dell (docs/robot_link.md)"
    if listening 8010; then
        echo "port 8010 is already forwarded: using it"
    else
        scripts/robot_link_tunnel.sh dell 8010 >"$LOGS/tunnel.log" 2>&1 &
        STARTED+=($!)
        echo "up (log: $LOGS/tunnel.log). The Dell's stack must be running: ssh dell, then ~/lbr-dell/turtlebot/run_robot_link.sh --amcl"
    fi
fi

if [[ $FACE == 1 ]]; then
    firefox --kiosk "http://127.0.0.1:$DISPLAY_PORT/" >/dev/null 2>&1 &
fi

TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
say "3/3  the robot"
echo "operator page:  http://127.0.0.1:$DISPLAY_PORT/operator${TS_IP:+   (from another computer on Tailscale: http://$TS_IP:$DISPLAY_PORT/operator)}"
echo "face page:      http://127.0.0.1:$DISPLAY_PORT/"
echo "The robot starts STOPPED: press Start on the operator page. Type quit (or Ctrl-C) to end the demo."
echo

python3 -m robot sim --live ${BASE:+--base "$BASE"} "${SIM_ARGS[@]}"
