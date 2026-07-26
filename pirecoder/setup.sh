#!/bin/bash
# =============================================================================
#  pirecoder — One-shot setup script for Raspberry Pi
#  Run: bash setup.sh
# =============================================================================

set -e  # exit on any error

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $1"; }
success() { echo -e "${GREEN}[OK]${RESET}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $1"; }
error()   { echo -e "${RED}[ERROR]${RESET} $1"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}══ $1 ══${RESET}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}${CYAN}"
echo "  ██████╗ ██╗██████╗ ███████╗ ██████╗ ██████╗ ██████╗ ███████╗██████╗ "
echo "  ██╔══██╗██║██╔══██╗██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗"
echo "  ██████╔╝██║██████╔╝█████╗  ██║     ██║   ██║██║  ██║█████╗  ██████╔╝"
echo "  ██╔═══╝ ██║██╔══██╗██╔══╝  ██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗"
echo "  ██║     ██║██║  ██║███████╗╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║"
echo "  ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝"
echo -e "${RESET}"
echo -e "  ${BOLD}Raspberry Pi Audio Recorder — Automated Setup${RESET}"
echo -e "  $(date '+%Y-%m-%d %H:%M:%S')\n"

# ── Config (edit these if needed) ─────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="audio-recorder"
SERVICE_USER="$(whoami)"
PORT=5000

# ── Step 0: Preflight checks ──────────────────────────────────────────────────
step "0 / 6  Preflight checks"

# Must not run as root
if [ "$EUID" -eq 0 ]; then
    error "Do not run this script as root. Run as your normal user (e.g. 'pi')."
fi

# Check we're on a Raspberry Pi / Debian-based system
if ! command -v apt &>/dev/null; then
    error "apt not found. This script is for Raspberry Pi OS (Debian-based)."
fi

# Check Python 3
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Please install Python 3.9+ first."
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    error "Python $PY_VER found but 3.9+ is required."
fi
success "Python $PY_VER found"

# Check app.py and requirements.txt are present
[ -f "$PROJECT_DIR/app.py" ]          || error "app.py not found in $PROJECT_DIR"
[ -f "$PROJECT_DIR/requirements.txt" ] || error "requirements.txt not found in $PROJECT_DIR"
[ -f "$PROJECT_DIR/templates/index.html" ] || error "templates/index.html not found in $PROJECT_DIR"
success "Project files found in $PROJECT_DIR"

# ── Step 1: System packages ───────────────────────────────────────────────────
step "1 / 6  Installing system packages"

info "Running apt update..."
sudo apt update -qq

info "Installing portaudio19-dev, python3-venv, python3-pip..."
sudo apt install -y portaudio19-dev python3-venv python3-pip alsa-utils

success "System packages installed"

# ── Step 2: Verify microphone ─────────────────────────────────────────────────
step "2 / 6  Checking microphone"

if arecord -l 2>/dev/null | grep -q "card"; then
    MIC_INFO=$(arecord -l 2>/dev/null | grep "card" | head -1)
    success "Microphone detected: $MIC_INFO"
else
    warn "No microphone detected by ALSA (arecord -l)."
    warn "Plug in a USB mic and re-run, or configure ~/.asoundrc manually."
    warn "Continuing setup — you can fix audio later."
fi

# ── Step 3: Python virtual environment ───────────────────────────────────────
step "3 / 6  Setting up Python virtual environment"

if [ -d "$VENV_DIR" ]; then
    info "Virtual environment already exists at $VENV_DIR — skipping creation."
else
    info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    success "Virtual environment created at $VENV_DIR"
fi

info "Upgrading pip..."
"$VENV_DIR/bin/pip" install --upgrade pip -q

info "Installing Python dependencies from requirements.txt..."
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

success "Python packages installed"
"$VENV_DIR/bin/pip" list | grep -E "Flask|pyaudio|eventlet|flask.socketio" || true

# ── Step 4: recordings directory ─────────────────────────────────────────────
step "4 / 6  Creating recordings directory"

mkdir -p "$PROJECT_DIR/recordings"
success "recordings/ directory ready at $PROJECT_DIR/recordings"

# ── Step 5: systemd service ───────────────────────────────────────────────────
step "5 / 6  Setting up systemd autostart service"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Writing $SERVICE_FILE ..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Pi Audio Recorder (pirecoder)
After=network.target sound.target

[Service]
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

sleep 2
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Service '$SERVICE_NAME' is running and enabled on boot"
else
    warn "Service did not start cleanly. Check logs with:"
    warn "  journalctl -u $SERVICE_NAME -n 30"
fi

# ── Step 6: Get Pi IP ─────────────────────────────────────────────────────────
step "6 / 6  Getting network address"

PI_IP=$(hostname -I | awk '{print $1}')

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║           ✓  Setup complete!                         ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Open this URL on any phone/browser on your Wi-Fi:${RESET}"
echo ""
echo -e "    ${BOLD}${YELLOW}http://${PI_IP}:${PORT}${RESET}"
echo ""
echo -e "  ${BOLD}Useful commands:${RESET}"
echo -e "    Check status :  ${CYAN}sudo systemctl status $SERVICE_NAME${RESET}"
echo -e "    View logs    :  ${CYAN}journalctl -u $SERVICE_NAME -f${RESET}"
echo -e "    Stop service :  ${CYAN}sudo systemctl stop $SERVICE_NAME${RESET}"
echo -e "    Start service:  ${CYAN}sudo systemctl start $SERVICE_NAME${RESET}"
echo -e "    Restart      :  ${CYAN}sudo systemctl restart $SERVICE_NAME${RESET}"
echo -e "    Disable boot :  ${CYAN}sudo systemctl disable $SERVICE_NAME${RESET}"
echo ""
echo -e "  ${BOLD}Recordings saved to:${RESET}  $PROJECT_DIR/recordings/"
echo ""
