#!/usr/bin/env bash
# ClaudeTV host installer — collector + master terminal as a systemd service.
#
#   curl -fsSL https://raw.githubusercontent.com/latticelabs-au/ClaudeTV/main/host/install.sh | bash
#
# Run on any always-on Linux box that has Claude Code installed AND logged in
# (the collector reads that box's Claude token; Claude Code keeps it fresh).
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/latticelabs-au/ClaudeTV/main/host"
DEST="${CLAUDETV_DIR:-$HOME/.claudetv}"
SERVICE="claude-usage"
PORT="${CLAUDETV_PORT:-8088}"

c(){ case "$2" in info)k="96";; ok)k="92";; warn)k="93";; err)k="91";; *)k="0";; esac; echo -e "\033[${k}m$1\033[0m"; }
die(){ c "✗ $1" err; exit 1; }

c "" ; c "  ClaudeTV — host installer" info ; c "  collector + master terminal · lattice labs" info ; c ""

# --- prerequisites ---
[ "$(uname)" = "Linux" ] || die "Linux only (the service uses systemd)."
command -v python3 >/dev/null || die "python3 not found — install Python 3.9+ and re-run."
command -v systemctl >/dev/null || die "systemd not found."
PY="$(command -v python3)"

if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then
  c "✓ Claude Code found" ok
else
  c "⚠ Claude Code not on PATH. Install it and run 'claude' once to log in," warn
  c "  otherwise usage will read 'expired' and stop updating." warn
fi

# --- fetch collector ---
mkdir -p "$DEST"
c "➤ Downloading collector to $DEST" info
curl -fsSL "$REPO_RAW/claude_usage_server.py" -o "$DEST/claude_usage_server.py" || die "download failed."
[ -f "$DEST/.env" ] || curl -fsSL "$REPO_RAW/.env.example" -o "$DEST/.env" 2>/dev/null || true

# --- systemd service (system unit, runs as the invoking user so it can read ~/.claude) ---
RUN_USER="${SUDO_USER:-$USER}"
UNIT="/etc/systemd/system/${SERVICE}.service"
c "➤ Installing systemd service (needs sudo)" info
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=ClaudeTV collector + master terminal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Environment=CLAUDETV_PORT=$PORT
WorkingDirectory=$DEST
ExecStart=$PY -u $DEST/claude_usage_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE" >/dev/null 2>&1 || sudo systemctl enable --now "$SERVICE"
sleep 2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if systemctl is-active --quiet "$SERVICE"; then
  c "" ; c "✓ ClaudeTV collector is running." ok
  c "" ; c "  Master terminal:  http://${IP:-<host-ip>}:${PORT}/" info
  c "  Paste this into the device's Collector URL:  http://${IP:-<host-ip>}:${PORT}/usage" info
  c "" ; c "  Manage it from the terminal — no SSH needed." ""
else
  c "✗ Service failed to start. Check: journalctl -u $SERVICE -e" err; exit 1
fi
