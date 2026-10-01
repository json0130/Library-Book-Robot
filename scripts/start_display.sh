#!/usr/bin/env bash
# Start the robot face server and open it full-screen in a browser (kiosk mode).
# Usage: scripts/start_display.sh [port]     (type emotions in this terminal; quit or Ctrl+C stops everything)
set -uo pipefail

PORT="${1:-8765}"
URL="http://localhost:${PORT}"
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
            exec "$b" --kiosk --noerrdialogs --disable-infobars --no-first-run "$URL"
        fi
    done
    if command -v firefox >/dev/null; then
        exec firefox --kiosk "$URL"
    fi
    echo "No browser found (chromium, chromium-browser, firefox); open $URL manually." >&2
) &
BROWSER=$!
trap 'kill "$BROWSER" 2>/dev/null' EXIT

python3 main.py display --port "$PORT"
