#!/usr/bin/env bash
# Run this ON the Oracle Cloud instance, over SSH. Detects Oracle Linux
# (dnf/firewalld, default user "opc") vs Ubuntu (apt/ufw, default user
# "ubuntu") and uses the right package manager and firewall tool for each.
#
#   ssh opc@<your-instance-ip>          # Oracle Linux
#   ssh ubuntu@<your-instance-ip>       # Ubuntu
#   git clone https://github.com/shaykashif/alpintage.git
#   cd alpintage
#   bash deploy/setup.sh
#
# This script: installs git if missing, installs uv, syncs the Python
# environment, prompts you to fill in .env, installs the two systemd
# services (paper-only loop + dashboard), and opens the dashboard port in
# the instance's own firewall. It does NOT touch Oracle Cloud's separate
# cloud-level firewall (Security List / Network Security Group) -- that's a
# console/account-level setting you have to open yourself; see DEPLOY.md.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
echo "Working in $REPO_DIR"

# --- detect OS family ---
OS_FAMILY="unknown"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in
    *ol*|*rhel*|*fedora*) OS_FAMILY="rhel" ;;
    *ubuntu*|*debian*)    OS_FAMILY="debian" ;;
  esac
fi
echo "Detected OS family: $OS_FAMILY (${PRETTY_NAME:-unknown})"

# --- base packages (git) ---
if ! command -v git >/dev/null 2>&1; then
  echo "Installing git..."
  if [ "$OS_FAMILY" = "rhel" ]; then
    sudo dnf install -y git
  elif [ "$OS_FAMILY" = "debian" ]; then
    sudo apt update && sudo apt install -y git
  else
    echo "Unrecognized OS -- install git yourself, then re-run this script."
    exit 1
  fi
fi

# --- uv (same install method on every distro, covers aarch64/Ampere) ---
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

# --- SELinux: let systemd actually read .env as an EnvironmentFile ---
# On Oracle Linux (SELinux enforcing by default), a file under $HOME is
# labeled user_home_t, which systemd's domain is not permitted to read even
# though it's running as root -- root bypasses normal Unix permissions, not
# SELinux. Without this, both services fail with "Failed to load
# environment files: Permission denied" (confirmed live on a real deploy).
# Relabeling as etc_t, the standard label for config files systemd reads
# outside /etc itself, fixes it. A no-op if not on Oracle Linux, or if
# .env is already outside $HOME.
if [ "$OS_FAMILY" = "rhel" ]; then
  if ! command -v semanage >/dev/null 2>&1; then
    sudo dnf install -y policycoreutils-python-utils
  fi
  echo "Relabeling .env for SELinux (etc_t) so systemd can read it..."
  sudo semanage fcontext -a -t etc_t "$REPO_DIR/.env" 2>/dev/null || \
    sudo semanage fcontext -m -t etc_t "$REPO_DIR/.env"
  sudo restorecon -v "$REPO_DIR/.env"

  # --- SELinux: let systemd actually EXECUTE the Python interpreter ---
  # A second, separate denial from the one above: .venv/bin/python is a
  # SYMLINK uv creates pointing at its own managed Python build under
  # ~/.local/share/uv/python/... -- also inside $HOME, so also user_home_t.
  # SELinux checks the symlink's TARGET when enforcing execute permission,
  # so relabeling only .venv/bin does nothing; the real interpreter needs
  # it too. Without this, the service fails with status=203/EXEC (confirmed
  # live). bin_t is the standard label used throughout /usr/bin, /bin, etc.
  # This affects every uv-managed systemd service on Oracle Linux, not just
  # this project.
  UV_PYTHON_DIR="$HOME/.local/share/uv/python"
  if [ -d "$UV_PYTHON_DIR" ]; then
    echo "Relabeling uv's managed Python install for SELinux (bin_t)..."
    sudo semanage fcontext -a -t bin_t "$UV_PYTHON_DIR(/.*)?" 2>/dev/null || \
      sudo semanage fcontext -m -t bin_t "$UV_PYTHON_DIR(/.*)?"
    sudo restorecon -R -v "$UV_PYTHON_DIR"
  fi
  sudo semanage fcontext -a -t bin_t "$REPO_DIR/.venv/bin(/.*)?" 2>/dev/null || \
    sudo semanage fcontext -m -t bin_t "$REPO_DIR/.venv/bin(/.*)?"
  sudo restorecon -R -v "$REPO_DIR/.venv"
fi

# --- systemd services ---
echo "Installing systemd services..."
sed "s#/opt/alpintage#$REPO_DIR#g" deploy/kalshi-loop.service | sudo tee /etc/systemd/system/kalshi-loop.service >/dev/null
sed "s#/opt/alpintage#$REPO_DIR#g" deploy/kalshi-dashboard.service | sudo tee /etc/systemd/system/kalshi-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-loop.service
sudo systemctl enable --now kalshi-dashboard.service

# --- instance-level firewall ---
if [ "$OS_FAMILY" = "rhel" ] && command -v firewall-cmd >/dev/null 2>&1; then
  echo "Opening port 8080 via firewalld..."
  sudo firewall-cmd --permanent --add-port=8080/tcp
  sudo firewall-cmd --reload
elif command -v ufw >/dev/null 2>&1; then
  echo "Opening port 8080 via ufw..."
  sudo ufw allow 8080/tcp || true
else
  echo "No firewalld or ufw found -- falling back to raw iptables."
  sudo iptables -C INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
  sudo netfilter-persistent save 2>/dev/null || true
fi

echo
echo "Done. Check status with:"
echo "  systemctl status kalshi-loop.service"
echo "  systemctl status kalshi-dashboard.service"
echo "  journalctl -u kalshi-loop.service -f"
echo
echo "IMPORTANT: the dashboard also needs port 8080 opened in Oracle Cloud's"
echo "own console -- Security List or Network Security Group -- this script"
echo "cannot do that part. See DEPLOY.md."
echo
if [ "$OS_FAMILY" = "rhel" ]; then
  echo "Oracle Linux ships SELinux in enforcing mode. If the dashboard is"
  echo "unreachable even after the firewall and OCI console steps, check:"
  echo "  sudo ausearch -m avc -ts recent"
fi
