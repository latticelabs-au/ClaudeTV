#!/usr/bin/env bash
# ClaudeTV collector + master terminal installer (systemd).
# Run on the always-on host that has Claude Code logged in:  sudo bash install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
PY="$(command -v python3)"
UNIT=/etc/systemd/system/claude-usage.service

[ -n "$PY" ] || { echo "python3 not found"; exit 1; }
[ -f "$DIR/.env" ] || { cp "$DIR/.env.example" "$DIR/.env"; echo "created $DIR/.env (edit in the terminal)"; }

cat > "$UNIT" <<EOF
[Unit]
Description=ClaudeTV collector + master terminal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$PY -u $DIR/claude_usage_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now claude-usage.service
sleep 2
systemctl --no-pager --full status claude-usage.service | head -5 || true
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Installed. Master terminal:  http://${IP:-<host-ip>}:8088/"
echo "Point the firmware's USAGE_URL at  http://${IP:-<host-ip>}:8088/usage"
