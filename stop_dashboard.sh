#!/usr/bin/env bash
# One-line stop for the live CPX dashboard. Matches by script name, not an
# exact flag list -- see README.md's "Quick reference" section for why
# (there should only ever be one instance, since the CPX serial port can't
# be shared, and matching specific flags is fragile the moment the start
# command's flags change).
set -uo pipefail

if ! pgrep -f "cpx_dashboard\.py" > /dev/null 2>&1; then
    echo "Not running."
    exit 0
fi

echo "Stopping cpx_dashboard.py (PID(s): $(pgrep -f 'cpx_dashboard\.py' | tr '\n' ' '))..."
sudo pkill -f "cpx_dashboard\.py"
sleep 1.5

if pgrep -f "cpx_dashboard\.py" > /dev/null 2>&1; then
    echo "Still running -- forcing..." >&2
    sudo pkill -9 -f "cpx_dashboard\.py"
    sleep 1
fi

if pgrep -f "cpx_dashboard\.py" > /dev/null 2>&1; then
    echo "Failed to stop it -- check manually: ps aux | grep cpx_dashboard" >&2
    exit 1
else
    echo "Stopped. Port 80 and the CPX serial port should now be free."
fi
