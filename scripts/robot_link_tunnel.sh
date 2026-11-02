#!/bin/bash
# The ssh tunnel to the Dell, kept up: Jetson 127.0.0.1:8010 -> Dell 127.0.0.1:8010 (robot_link_api started with --local).
# The Dell's firewall blocks port 8010 from the Wi-Fi; ssh (port 22) is allowed, so the link rides inside it. Nothing but this
# tunnel can reach the API, whatever the token. See docs/robot_link.md, section 5.1.
#
#     scripts/robot_link_tunnel.sh              run in the foreground (Ctrl-C stops it); leave it running next to the robot
#     scripts/robot_link_tunnel.sh dell 8010    the ssh Host (from ~/.ssh/config) and the port; these are the defaults
#
# ssh gives up within about 6 s of a dead connection (ServerAliveInterval 2 x ServerAliveCountMax 2), and this script starts it
# again after a second. While the tunnel is down the robot's link reads "lost" and the Dell stops by itself after 3 s without a heartbeat.
# Needs key-based ssh to the Dell (ssh-copy-id), so that it can restart without anyone typing a password.

HOST="${1:-dell}"
PORT="${2:-8010}"
RETRY_S="${ROBOT_LINK_TUNNEL_RETRY_S:-1}"

stop=0
trap 'stop=1; [ -n "$PID" ] && kill "$PID" 2>/dev/null' INT TERM

while [ $stop -eq 0 ]; do
    echo "$(date '+%H:%M:%S') tunnel: 127.0.0.1:$PORT -> $HOST:127.0.0.1:$PORT"
    ssh -N -o ServerAliveInterval=2 -o ServerAliveCountMax=2 -o ExitOnForwardFailure=yes -o BatchMode=yes \
        -L "$PORT:127.0.0.1:$PORT" "$HOST" &
    PID=$!
    wait "$PID"
    code=$?
    [ $stop -eq 1 ] && break
    echo "$(date '+%H:%M:%S') tunnel: ssh ended (exit $code), starting again in ${RETRY_S}s"
    sleep "$RETRY_S" &
    wait $!
done
echo "tunnel: stopped"
