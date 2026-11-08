#!/usr/bin/env bash
# Update the ClaudeTV collector on this host to the latest committed version and restart it.
#
# No sudo needed: the systemd service runs as this user with Restart=always, so signalling the
# running process makes systemd relaunch it with the freshly-pulled code. Run from the clone:
#   ~/ClaudeTV/host/update.sh
#
# The collector's config + state (.env, notify_state.json, resets.log) live in ~/.claudetv and are
# gitignored, so a pull never touches them.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
echo "-> git pull"
git pull --ff-only
PID="$(systemctl show -p MainPID --value claude-usage 2>/dev/null || echo 0)"
if [ "${PID:-0}" != "0" ]; then
  kill "$PID" && echo "-> restarted claude-usage (was pid $PID; systemd relaunches with the new code)"
else
  echo "-> claude-usage not running under systemd; restart it however you normally do"
fi
