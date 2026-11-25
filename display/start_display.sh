#!/usr/bin/env bash
# Start the robot face server and open it full-screen in a browser (kiosk mode).
# Usage: display/start_display.sh [port]     (type emotions in this terminal; quit or Ctrl+C stops everything)
#        DISPLAY_UI=2 DISPLAY_THEME=light display/start_display.sh   the second look of the screen (see README), in light colours;
#        the operator page changes both while it runs
set -uo pipefail

PORT="${1:-8765}"
URL="http://localhost:${PORT}"
SHOW="${URL}/${DISPLAY_UI:+?ui=$DISPLAY_UI}"          # what the browser opens; the server is polled at ${URL}/
cd "$(dirname "$0")/.."

# Open the browser once the server answers. The server itself runs in the foreground below so it
# keeps the terminal's stdin (a background job would get /dev/null and could not read typed emotions).
(
    for _ in $(seq 1 50); do
        curl -fs -o /dev/null "$URL/" 2>/dev/null && break
        sleep 0.1
    done
    for b in chromium chromium-browser; do
        if command -v "$b" >/dev/null; then
            exec "$b" --kiosk --noerrdialogs --disable-infobars --no-first-run "$SHOW"
        fi
    done
    if command -v firefox >/dev/null; then
        exec firefox --kiosk "$SHOW"
    fi
    echo "No browser found (chromium, chromium-browser, firefox); open $URL manually." >&2
) &
BROWSER=$!
trap 'kill "$BROWSER" 2>/dev/null' EXIT

python3 -m display.server --port "$PORT" ${DISPLAY_UI:+--look "$DISPLAY_UI"} ${DISPLAY_THEME:+--theme "$DISPLAY_THEME"}
