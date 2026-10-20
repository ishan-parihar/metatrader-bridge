#!/usr/bin/env bash
set -uo pipefail

REAL_XAUTH=$(ls -t /run/user/1000/xauth_* 2>/dev/null | head -1)
export XAUTHORITY="${REAL_XAUTH:-$HOME/.Xauthority}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export DISPLAY=":0"

bottles-cli run -p terminal64 -b Apps -- &
BOTTLES_PID=$!

# Wait up to 30s for terminal64.exe to appear
MT5_PID=""
for i in $(seq 30); do
    MT5_PID=$(pgrep -f "terminal64.exe" | head -1)
    [ -n "$MT5_PID" ] && break
    sleep 1
done

# Wait for bottles-cli to exit (it always does after spawning wine)
wait $BOTTLES_PID 2>/dev/null || true

# If we found the MT5 process, block until it dies using tail --pid
if [ -n "$MT5_PID" ]; then
    tail --pid="$MT5_PID" -f /dev/null 2>/dev/null || true
fi
