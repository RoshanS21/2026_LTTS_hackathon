#!/usr/bin/env bash
# One-line start for the live CPX dashboard on port 80 (phone-reachable at
# http://timtam.box or http://10.42.0.1, no port needed in the URL).
# Wraps the command documented in README.md's "Quick reference: start /
# stop the phone-facing dashboard" -- see that section for the manual
# equivalent and for why cleanup checks the CPX serial port too.
set -euo pipefail
cd "$(dirname "$0")"

PORT=80
SERIAL_DEV="/dev/ttyACM0"
LOG_FILE="/tmp/cpx_dashboard.log"

if pgrep -f "cpx_dashboard\.py" > /dev/null 2>&1; then
    echo "Already running (PID(s): $(pgrep -f 'cpx_dashboard\.py' | tr '\n' ' '))." >&2
    echo "Run ./stop_dashboard.sh first if you want to restart it." >&2
    exit 1
fi

if sudo lsof -i ":$PORT" > /dev/null 2>&1; then
    echo "Port $PORT is already in use by something else:" >&2
    sudo lsof -i ":$PORT" >&2
    exit 1
fi

if [ -e "$SERIAL_DEV" ] && sudo lsof "$SERIAL_DEV" > /dev/null 2>&1; then
    echo "$SERIAL_DEV is already held by another process:" >&2
    sudo lsof "$SERIAL_DEV" >&2
    exit 1
fi

echo "Starting cpx_dashboard.py on port $PORT (log: $LOG_FILE)..."
sudo nohup python3 cpx_dashboard.py --gpio --no-open --http-port "$PORT" \
    > "$LOG_FILE" 2>&1 < /dev/null &
disown

sleep 2
if pgrep -f "cpx_dashboard\.py" > /dev/null 2>&1; then
    echo "Started (PID $(pgrep -f 'cpx_dashboard\.py' | tail -1))."
    echo "Phone: join WiFi 'timtam' (password maintain123), browse to http://timtam.box"
    echo "  (fallback: http://10.42.0.1 if the hostname doesn't resolve on some device)"
    echo "Logs: tail -f $LOG_FILE"
else
    echo "Failed to start -- last lines of $LOG_FILE:" >&2
    tail -20 "$LOG_FILE" >&2 || true
    exit 1
fi
