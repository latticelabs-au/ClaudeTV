#!/usr/bin/env bash
# ClaudeTV host installer — collector + master terminal as a systemd service.
# Idempotent (safe to re-run) and multi-distro (apt/dnf/yum/pacman/zypper/apk).
#
#   curl -fsSL https://raw.githubusercontent.com/latticelabs-au/ClaudeTV/main/host/install.sh | bash
#
# Run on any always-on Linux box. Claude Code is NOT required: the collector refreshes its
# own OAuth token. Log in once afterwards with `python3 ~/.claudetv/claude_usage_server.py --login`
# (skippable if the box already has a logged-in Claude Code install).
set -euo pipefail

REPO_RAW="${CLAUDETV_RAW:-https://raw.githubusercontent.com/latticelabs-au/ClaudeTV/main/host}"
DEST="${CLAUDETV_DIR:-$HOME/.claudetv}"
SERVICE="claude-usage"
PORT="${CLAUDETV_PORT:-8088}"

c(){ case "${2:-}" in info)k=96;; ok)k=92;; warn)k=93;; err)k=91;; *)k=0;; esac; printf '\033[%sm%s\033[0m\n' "$k" "$1"; }
die(){ c "✗ $1" err; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

c "" ; c "  ClaudeTV — host installer" info ; c "  collector + master terminal · lattice labs" info ; c ""

# --- root / sudo (works whether invoked as root or a normal user) ---
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  have sudo || die "Run as root, or install 'sudo'."
  SUDO="sudo"
fi

# --- platform ---
[ "$(uname -s)" = "Linux" ] || die "Linux only (this installer uses a systemd service)."

# --- package manager detection -> install python3 / curl only if missing ---
pkg_install(){
  if   have apt-get; then $SUDO apt-get update -qq && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
  elif have dnf;     then $SUDO dnf install -y -q "$@"
  elif have yum;     then $SUDO yum install -y -q "$@"
  elif have pacman;  then $SUDO pacman -Sy --noconfirm --needed "$@"
  elif have zypper;  then $SUDO zypper --non-interactive --quiet install "$@"
  elif have apk;     then $SUDO apk add --no-cache "$@"
  else return 1; fi
}
for dep in python3 curl; do
  have "$dep" && continue
  c "➤ Installing missing dependency: $dep" info
  pkg_install "$dep" || die "Could not auto-install '$dep' — install it manually and re-run."
done
have systemctl || die "systemd not found. Run the collector manually: python3 $DEST/claude_usage_server.py"
PY="$(command -v python3)"
c "✓ python3, curl, systemd present" ok

# --- credentials presence (warn only; Claude Code itself is NOT required) ---
if [ -f "$DEST/credentials.json" ] || [ -f "$HOME/.claude/.credentials.json" ]; then
  c "✓ Claude credentials found (the keeper will keep them fresh)" ok
else
  c "⚠ No Claude credentials yet. After install, log in once with:" warn
  c "    python3 $DEST/claude_usage_server.py --login" warn
fi

# --- idempotent: stop any existing instance before swapping files ---
$SUDO systemctl stop "$SERVICE" 2>/dev/null || true

# --- fetch collector (atomic) ; keep an existing .env (your saved config) ---
mkdir -p "$DEST"
c "➤ Downloading collector to $DEST" info
curl -fsSL "$REPO_RAW/claude_usage_server.py" -o "$DEST/.server.tmp" || die "Download failed (check network / URL)."
mv -f "$DEST/.server.tmp" "$DEST/claude_usage_server.py"
[ -f "$DEST/.env" ] || curl -fsSL "$REPO_RAW/.env.example" -o "$DEST/.env" 2>/dev/null || true

# --- systemd unit (runs as the invoking user so it can read ~/.claude) ---
RUN_USER="${SUDO_USER:-$USER}"
c "➤ Installing systemd service '$SERVICE' (user=$RUN_USER, port=$PORT)" info
$SUDO tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
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

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE"
sleep 2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if $SUDO systemctl is-active --quiet "$SERVICE"; then
  c "" ; c "✓ ClaudeTV collector is running." ok
  c "" ; c "  Master terminal:   http://${IP:-<host-ip>}:${PORT}/" info
  c "  Device Collector URL:  http://${IP:-<host-ip>}:${PORT}/usage" info
  c "" ; c "  Manage everything from the terminal — no SSH needed." ""
else
  c "✗ Service failed to start. Logs:  journalctl -u $SERVICE -e --no-pager" err
  exit 1
fi
