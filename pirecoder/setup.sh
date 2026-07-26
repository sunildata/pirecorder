#!/bin/bash
# =============================================================================
#  pirecoder — Smart auto-setup for Raspberry Pi
#  Checks every requirement first, installs only what is missing, skips the rest
#  Run: bash setup.sh
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

ok()      { echo -e "  ${GREEN}✔${RESET}  $1"; }
skip()    { echo -e "  ${DIM}–  $1 (already present, skipping)${RESET}"; }
info()    { echo -e "  ${CYAN}→${RESET}  $1"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail()    { echo -e "\n  ${RED}✘  ERROR: $1${RESET}\n"; exit 1; }
plan_add(){ echo -e "  ${YELLOW}+${RESET}  $1"; NEEDS_WORK=true; }
section() {
    echo ""
    echo -e "${BOLD}${CYAN}┌─────────────────────────────────────────────────────┐${RESET}"
    printf "${BOLD}${CYAN}│  %-51s│${RESET}\n" "$1"
    echo -e "${BOLD}${CYAN}└─────────────────────────────────────────────────────┘${RESET}"
}

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="audio-recorder"
SERVICE_USER="$(whoami)"
PORT=5000

# Track results
INSTALLED=()
SKIPPED=()
WARNINGS=()

# Need-flags — use strings, never run them as commands
NEED_APT_UPDATE="false"
NEED_PORTAUDIO="false"
NEED_VENV_PKG="false"
NEED_PIP_PKG="false"
NEED_ALSA_UTILS="false"
NEED_VENV="false"
NEED_PY_DEPS="false"
NEED_RECORDINGS_DIR="false"
NEED_SERVICE="false"
NEEDS_WORK=false   # plain boolean for the plan gate

# ── Banner ────────────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}${CYAN}"
echo "   ____  _ ____                   _             "
echo "  |  _ \(_)  _ \ ___  ___ ___  __| | ___ _ __  "
echo "  | |_) | | |_) / _ \/ __/ _ \/ _\` |/ _ \ '__| "
echo "  |  __/| |  _ <  __/ (_| (_) | (_| |  __/ |    "
echo "  |_|   |_|_| \_\___|\___\___/ \__,_|\___|_|    "
echo -e "${RESET}"
echo -e "  ${BOLD}Smart Auto-Setup — checks before installing${RESET}"
echo -e "  ${DIM}$(date '+%A, %d %b %Y  %H:%M:%S')  |  user: ${SERVICE_USER}${RESET}"
echo -e "  ${DIM}project: ${PROJECT_DIR}${RESET}"

# =============================================================================
# CHECK PHASE
# =============================================================================
section "CHECK PHASE — scanning your system"

# ── 1. Not root ───────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    fail "Do not run as root. Use your normal user (e.g. 'pi')."
fi
ok "Running as non-root user '${SERVICE_USER}'"

# ── 2. apt ────────────────────────────────────────────────────────────────────
if ! command -v apt &>/dev/null; then
    fail "apt not found. This script requires Raspberry Pi OS (Debian-based)."
fi
ok "apt package manager found"

# ── 3. Python 3.9+ ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.9+ first."
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    fail "Python $PY_VER detected — 3.9+ required."
fi
ok "Python $PY_VER"

# ── 4. Project files ─────────────────────────────────────────────────────────
[ -f "$PROJECT_DIR/app.py" ]               || fail "app.py not found in $PROJECT_DIR"
[ -f "$PROJECT_DIR/requirements.txt" ]      || fail "requirements.txt not found in $PROJECT_DIR"
[ -f "$PROJECT_DIR/templates/index.html" ]  || fail "templates/index.html not found in $PROJECT_DIR"
ok "Project files present (app.py, requirements.txt, templates/index.html)"

# ── 5. portaudio19-dev ───────────────────────────────────────────────────────
if dpkg -s portaudio19-dev &>/dev/null; then
    skip "portaudio19-dev"
    SKIPPED+=("portaudio19-dev")
else
    warn "portaudio19-dev — NOT installed"
    NEED_PORTAUDIO="true"
    NEED_APT_UPDATE="true"
fi

# ── 6. python3-venv ──────────────────────────────────────────────────────────
if dpkg -s python3-venv &>/dev/null; then
    skip "python3-venv"
    SKIPPED+=("python3-venv")
else
    warn "python3-venv — NOT installed"
    NEED_VENV_PKG="true"
    NEED_APT_UPDATE="true"
fi

# ── 7. python3-pip ───────────────────────────────────────────────────────────
if command -v pip3 &>/dev/null || dpkg -s python3-pip &>/dev/null; then
    skip "python3-pip"
    SKIPPED+=("python3-pip")
else
    warn "python3-pip — NOT installed"
    NEED_PIP_PKG="true"
    NEED_APT_UPDATE="true"
fi

# ── 8. alsa-utils ────────────────────────────────────────────────────────────
if command -v arecord &>/dev/null; then
    skip "alsa-utils"
    SKIPPED+=("alsa-utils")
else
    warn "alsa-utils — NOT installed"
    NEED_ALSA_UTILS="true"
    NEED_APT_UPDATE="true"
fi

# ── 9. Virtual environment ───────────────────────────────────────────────────
if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    skip "Python virtual environment (venv/)"
    SKIPPED+=("venv")
else
    warn "Virtual environment — NOT found at $VENV_DIR"
    NEED_VENV="true"
fi

# ── 10. Python packages ───────────────────────────────────────────────────────
if [ -f "$VENV_DIR/bin/pip" ]; then
    MISSING_PY=()
    for pkg in Flask flask-socketio pyaudio; do
        if ! "$VENV_DIR/bin/pip" show "$pkg" &>/dev/null; then
            MISSING_PY+=("$pkg")
        fi
    done
    if [ ${#MISSING_PY[@]} -eq 0 ]; then
        skip "Python packages (Flask, flask-socketio, pyaudio)"
        SKIPPED+=("python-packages")
    else
        warn "Missing Python packages: ${MISSING_PY[*]}"
        NEED_PY_DEPS="true"
    fi
else
    warn "Python packages — venv not ready yet, will install after venv creation"
    NEED_PY_DEPS="true"
fi

# ── 11. recordings/ ──────────────────────────────────────────────────────────
if [ -d "$PROJECT_DIR/recordings" ]; then
    skip "recordings/ directory"
    SKIPPED+=("recordings-dir")
else
    warn "recordings/ directory — NOT found"
    NEED_RECORDINGS_DIR="true"
fi

# ── 12. systemd service — always rewrite to guarantee correct paths ───────────
NEED_SERVICE="true"
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    warn "Service running — will rewrite service file and restart to ensure correct config"
else
    if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
        warn "Service file exists but not running — will rewrite and restart"
    else
        warn "systemd service '$SERVICE_NAME' — NOT installed"
    fi
fi

# ── 13. Microphone (informational, non-blocking) ─────────────────────────────
if command -v arecord &>/dev/null; then
    if arecord -l 2>/dev/null | grep -q "card" 2>/dev/null || true; then
        MIC_LINE=$(arecord -l 2>/dev/null | grep "card" | head -1 || true)
        if [ -n "$MIC_LINE" ]; then
            ok "Microphone detected: $MIC_LINE"
        else
            warn "No microphone found — plug in USB mic before recording"
            WARNINGS+=("No microphone detected")
        fi
    fi
fi

# ── 14. Firewall / port check (informational) ─────────────────────────────────
if command -v ufw &>/dev/null; then
    UFW_STATUS=$(sudo ufw status 2>/dev/null | head -1 || true)
    if echo "$UFW_STATUS" | grep -q "active"; then
        if sudo ufw status 2>/dev/null | grep -q "$PORT"; then
            ok "Firewall: port $PORT is allowed in ufw"
        else
            warn "ufw is active but port $PORT may be blocked"
            WARNINGS+=("ufw active — run: sudo ufw allow $PORT if phone can't connect")
        fi
    else
        ok "Firewall (ufw) inactive — port $PORT accessible"
    fi
fi

# =============================================================================
# PLAN PHASE — show what will be done, ask to confirm
# =============================================================================
section "PLAN — what will be installed / created"

if [ "$NEED_APT_UPDATE"      = "true" ]; then plan_add "apt update"; fi
if [ "$NEED_PORTAUDIO"       = "true" ]; then plan_add "apt install portaudio19-dev"; fi
if [ "$NEED_VENV_PKG"        = "true" ]; then plan_add "apt install python3-venv"; fi
if [ "$NEED_PIP_PKG"         = "true" ]; then plan_add "apt install python3-pip"; fi
if [ "$NEED_ALSA_UTILS"      = "true" ]; then plan_add "apt install alsa-utils"; fi
if [ "$NEED_VENV"            = "true" ]; then plan_add "create Python virtual environment (venv/)"; fi
if [ "$NEED_PY_DEPS"         = "true" ]; then plan_add "pip install -r requirements.txt"; fi
if [ "$NEED_RECORDINGS_DIR"  = "true" ]; then plan_add "mkdir recordings/"; fi
plan_add "rewrite + restart systemd service '$SERVICE_NAME' (always ensures correct boot config)"

if [ "$NEEDS_WORK" = false ]; then
    echo -e "  ${GREEN}✔  Everything is already set up — nothing to do!${RESET}"
    echo ""
    PI_IP=$(hostname -I | awk '{print $1}')
    echo -e "  ${BOLD}Your recorder is live at:${RESET}  ${BOLD}${YELLOW}http://${PI_IP}:${PORT}${RESET}"
    echo ""
    exit 0
fi

echo ""
read -r -p "  Proceed with the above? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
fi

# =============================================================================
# INSTALL PHASE
# =============================================================================
section "INSTALL PHASE"

# ── apt update ────────────────────────────────────────────────────────────────
if [ "$NEED_APT_UPDATE" = "true" ]; then
    info "Running apt update..."
    sudo apt update -qq
    ok "apt updated"
fi

# ── apt packages ──────────────────────────────────────────────────────────────
APT_PKGS=()
if [ "$NEED_PORTAUDIO"  = "true" ]; then APT_PKGS+=("portaudio19-dev"); fi
if [ "$NEED_VENV_PKG"   = "true" ]; then APT_PKGS+=("python3-venv"); fi
if [ "$NEED_PIP_PKG"    = "true" ]; then APT_PKGS+=("python3-pip"); fi
if [ "$NEED_ALSA_UTILS" = "true" ]; then APT_PKGS+=("alsa-utils"); fi

if [ ${#APT_PKGS[@]} -gt 0 ]; then
    info "Installing system packages: ${APT_PKGS[*]}"
    sudo apt install -y -qq "${APT_PKGS[@]}"
    for pkg in "${APT_PKGS[@]}"; do
        ok "Installed: $pkg"
        INSTALLED+=("$pkg")
    done
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if [ "$NEED_VENV" = "true" ]; then
    info "Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created"
    INSTALLED+=("venv")
fi

# ── pip upgrade ───────────────────────────────────────────────────────────────
info "Ensuring pip is up to date inside venv..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
ok "pip is up to date"

# ── Remove eventlet if installed (breaks Python 3.13) ────────────────────────
if "$VENV_DIR/bin/pip" show eventlet &>/dev/null; then
    info "Removing eventlet (incompatible with Python 3.13)..."
    "$VENV_DIR/bin/pip" uninstall -y eventlet -q
    ok "eventlet removed"
    INSTALLED+=("removed-eventlet")
fi

# ── Python packages ───────────────────────────────────────────────────────────
if [ "$NEED_PY_DEPS" = "true" ]; then
    info "Installing Python packages from requirements.txt..."
    "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
    ok "Python packages installed"
    INSTALLED+=("python-packages")
fi

# ── recordings/ ──────────────────────────────────────────────────────────────
if [ "$NEED_RECORDINGS_DIR" = "true" ]; then
    mkdir -p "$PROJECT_DIR/recordings"
    ok "Created recordings/ directory"
    INSTALLED+=("recordings-dir")
fi

# ── systemd service ───────────────────────────────────────────────────────────
if [ "$NEED_SERVICE" = "true" ]; then
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
Environment=PYTHONUNBUFFERED=1
SupplementaryGroups=audio

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable -q "$SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"

    # Poll up to 15 seconds for the service to reach active or failed
    info "Waiting for service to start..."
    SVC_FINAL="unknown"
    for i in $(seq 1 15); do
        sleep 1
        SVC_FINAL=$(sudo systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
        if [ "$SVC_FINAL" = "active" ]; then
            ok "Service '$SERVICE_NAME' is active and enabled on boot"
            INSTALLED+=("systemd-service")
            break
        elif [ "$SVC_FINAL" = "failed" ]; then
            break
        fi
        printf "  ${CYAN}→${RESET}  still starting... (%ss)\r" "$i"
    done

    if [ "$SVC_FINAL" != "active" ]; then
        echo ""
        warn "Service did not reach active state (status: $SVC_FINAL)"
        warn "Check logs: journalctl -u $SERVICE_NAME -n 20 --no-pager"
        WARNINGS+=("Service status: $SVC_FINAL — check journalctl -u $SERVICE_NAME")
    fi
fi

# ── Post-install mic recheck ──────────────────────────────────────────────────
if command -v arecord &>/dev/null; then
    if ! arecord -l 2>/dev/null | grep -q "card" 2>/dev/null; then
        WARNINGS+=("No microphone detected — plug in a USB mic then: sudo systemctl restart $SERVICE_NAME")
    fi
fi

# =============================================================================
# SUMMARY
# =============================================================================
PI_IP=$(hostname -I | awk '{print $1}')
SVC_STATUS=$(sudo systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "unknown")

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║              ✓  Setup complete!                          ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""

if [ ${#INSTALLED[@]} -gt 0 ]; then
    echo -e "  ${BOLD}Installed / created:${RESET}"
    for item in "${INSTALLED[@]}"; do
        echo -e "    ${GREEN}✔${RESET}  $item"
    done
    echo ""
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
    echo -e "  ${BOLD}Already present (skipped):${RESET}"
    for item in "${SKIPPED[@]}"; do
        echo -e "    ${DIM}–  $item${RESET}"
    done
    echo ""
fi

if [ ${#WARNINGS[@]} -gt 0 ]; then
    echo -e "  ${BOLD}${YELLOW}Warnings:${RESET}"
    for w in "${WARNINGS[@]}"; do
        echo -e "    ${YELLOW}⚠${RESET}  $w"
    done
    echo ""
fi

echo -e "  ${BOLD}Service status:${RESET}  $SVC_STATUS"
echo ""
echo -e "  ${BOLD}Open on any phone / browser on your Wi-Fi:${RESET}"
echo ""
echo -e "    ${BOLD}${YELLOW}http://${PI_IP}:${PORT}${RESET}"
echo ""
echo -e "  ${BOLD}Useful commands:${RESET}"
echo -e "    ${DIM}sudo systemctl status  $SERVICE_NAME${RESET}"
echo -e "    ${DIM}sudo systemctl restart $SERVICE_NAME${RESET}"
echo -e "    ${DIM}journalctl -u $SERVICE_NAME -f${RESET}"
echo ""
