#!/usr/bin/env bash
# Run this ON the Oracle Cloud instance, over SSH. Written for Ubuntu
# (Oracle's most common free-tier image); if you're on Oracle Linux, swap
# `apt` for `dnf` in the one line below.
#
#   ssh ubuntu@<your-instance-ip>
#   git clone https://github.com/shaykashif/alpintage.git /opt/alpintage  # or sudo, see below
#   cd /opt/alpintage
#   bash deploy/setup.sh
#
# This script: installs uv, syncs the Python environment, prompts you to
# fill in .env, installs the two systemd services (paper-only loop +
# dashboard), and opens the dashboard port in the instance's own firewall.
# It does NOT touch Oracle Cloud's separate cloud-level firewall (Security
# List / Network Security Group) -- that's a console/account-level setting
# you have to open yourself; see DEPLOY.md.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
echo "Working in $REPO_DIR"

# --- uv ---
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

# --- python env ---
echo "Syncing dependencies (uv sync)..."
uv sync

# --- .env ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env from .env.example -- edit it now and fill in:"
  echo "  TYPESAFE_API_KEY, ODDS_API_KEY"
  echo "Press Enter once you've saved it (nano .env, then Ctrl+O, Enter, Ctrl+X)."
  read -r
fi

# --- systemd services ---
echo "Installing systemd services..."
sudo sed "s#/opt/alpintage#$REPO_DIR#g" deploy/kalshi-loop.service | sudo tee /etc/systemd/system/kalshi-loop.service >/dev/null
sudo sed "s#/opt/alpintage#$REPO_DIR#g" deploy/kalshi-dashboard.service | sudo tee /etc/systemd/system/kalshi-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-loop.service
sudo systemctl enable --now kalshi-dashboard.service

# --- instance-level firewall (ufw) ---
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 8080/tcp || true
fi
# Oracle images also often use iptables directly instead of/alongside ufw.
sudo iptables -C INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

echo
echo "Done. Check status with:"
echo "  systemctl status kalshi-loop.service"
echo "  systemctl status kalshi-dashboard.service"
echo "  journalctl -u kalshi-loop.service -f"
echo
echo "IMPORTANT: the dashboard also needs port 8080 opened in Oracle Cloud's"
echo "own console -- Security List or Network Security Group -- this script"
echo "cannot do that part. See DEPLOY.md."
