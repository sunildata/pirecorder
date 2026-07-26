#!/bin/bash
# =============================================================================
#  pirecoder — Smart auto-setup for Raspberry Pi
#  Installs directly to system Python (no venv)
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
SERVICE_NAME="audio-recorder"
SERVICE_USER="$(whoami)"
PORT=5000
PYTHON_BIN="$(command -v python3)"
PIP_BIN="$(command -v pip3 2>/dev/null || echo "")"

# Track results
INSTALLED=()
SKIPPED=()
WARNINGS=()

# Need-flags
NEED_APT_UPDATE="false"
NEED_PORTAUDIO="false"
NEED_PIP_PKG="false"
NEED_ALSA_UTILS="false"
NEED_PY_DEPS="false"
NEED_RECORDINGS_DIR="false"
NEEDS_WORK=false

# ── Banner ────────────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}${CYAN}"
echo "   ____  _ ____                   _             "
echo "  |  _ \(_)  _ \ ___  ___ ___  __| | ___ _ __  "
echo "  | |_) | | |_) / _ \/ __/ _ \/ _\` |/ _ \ '__| "
echo "  |  __/| |  _ <  __/ (_| (_) | (_| |  __/ |    "
echo "  |_|   |_|_| \_\___|\___\___/ \__,_|\___|_|    "
echo -e "${RESET}"
echo -e "  ${BOLD}Smart Auto-Setup — system Python, no venv${RESET}"
echo -e "  ${DIM}$(date '+%A, %d %b %Y  %H:%M:%S')  |  user: ${SERVICE_USER}${RESET}"
echo -e "  ${DIM}project: ${PROJECT_DIR}${RESET}"

# =============================================================================
# CHECK PHASE
# =============================================================================
section "CHECK PHASE — scanning your system"

# ── 1. Not root ───────────────────────────────────────────────────────────────
[ "$EUID" -eq 0 ] && fail "Do not run as root. Use your normal user (e.g. 'pi')."
ok "Running as non-root user '${SERVICE_USER}'"

# ── 2. apt ────────────────────────────────────────────────────────────────────
command -v apt &>/dev/null || fail "apt not found — requires Raspberry Pi OS."
ok "apt package manager found"

# ── 3. Python 3.9+ ───────────────────────────────────────────────────────────
command -v python3 &>/dev/null || fail "python3 not found."
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
{ [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; } \
    && fail "Python $PY_VER — 3.9+ required."
ok "Python $PY_VER  ($PYTHON_BIN)"

# ── 4. Project files ─────────────────────────────────────────────────────────
[ -f "$PROJECT_DIR/app.py" ]              || fail "app.py not found in $PROJECT_DIR"
[ -f "$PROJECT_DIR/requirements.txt" ]    || fail "requirements.txt not found"
[ -f "$PROJECT_DIR/templates/index.html" ] || fail "templates/index.html not found"
ok "Project files present"

# ── 5. portaudio19-dev ───────────────────────────────────────────────────────
if dpkg -s portaudio19-dev &>/dev/null; then
    skip "portaudio19-dev"; SKIPPED+=("portaudio19-dev")
else
    warn "portaudio19-dev — NOT installed"
    NEED_PORTAUDIO="true"; NEED_APT_UPDATE="true"
fi

# ── 6. python3-pip ───────────────────────────────────────────────────────────
if command -v pip3 &>/dev/null || dpkg -s python3-pip &>/dev/null; then
    skip "python3-pip"; SKIPPED+=("python3-pip")
else
    warn "python3-pip — NOT installed"
    NEED_PIP_PKG="true"; NEED_APT_UPDATE="true"
fi

# ── 7. alsa-utils ────────────────────────────────────────────────────────────
if command -v arecord &>/dev/null; then
    skip "alsa-utils"; SKIPPED+=("alsa-utils")
else
    warn "alsa-utils — NOT installed"
    NEED_ALSA_UTILS="true"; NEED_APT_UPDATE="true"
fi

# ── 8. Python packages (system) ──────────────────────────────────────────────
check_pkg() { python3 -c "import $1" &>/dev/null; }
MISSING_PY=()
for mod in flask flask_socketio pyaudio; do
    check_pkg "$mod" || MISSING_PY+=("$mod")
done
if [ ${#MISSING_PY[@]} -eq 0 ]; then
    skip "Python packages (flask, flask_socketio, pyaudio)"; SKIPPED+=("python-packages")
else
    warn "Missing Python modules: ${MISSING_PY[*]}"
    NEED_PY_DEPS="true"
fi

# ── 9. Remove leftover eventlet ───────────────────────────────────────────────
if python3 -c "import eventlet" &>/dev/null; then
    warn "eventlet found — will remove (incompatible with Python 3.13)"
    NEED_PY_DEPS="true"
fi

# ── 10. recordings/ ──────────────────────────────────────────────────────────
if [ -d "$PROJECT_DIR/recordings" ]; then
    skip "recordings/ directory"; SKIPPED+=("recordings-dir")
else
    warn "recordings/ directory — NOT found"; NEED_RECORDINGS_DIR="true"
fi

# ── 11. Microphone ───────────────────────────────────────────────────────────
if command -v arecord &>/dev/null; then
    MIC_LINE=$(arecord -l 2>/dev/null | grep "card" | head -1 || true)
    if [ -n "$MIC_LINE" ]; then
        ok "Microphone: $MIC_LINE"
    else
        warn "No microphone found — plug in USB mic before recording"
        WARNINGS+=("No microphone detected")
    fi
fi

# ── 12. Firewall ─────────────────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    UFW_STATUS=$(sudo ufw status 2>/dev/null | head -1 || true)
    if echo "$UFW_STATUS" | grep -q "active"; then
        sudo ufw status 2>/dev/null | grep -q "$PORT" \
            && ok "Firewall: port $PORT allowed" \
            || { warn "ufw active — port $PORT may be blocked"
                 WARNINGS+=("Run: sudo ufw allow $PORT"); }
    fi
fi

# =============================================================================
# PLAN PHASE
# =============================================================================
section "PLAN — what will be installed"

if [ "$NEED_APT_UPDATE"     = "true" ]; then plan_add "apt update"; fi
if [ "$NEED_PORTAUDIO"      = "true" ]; then plan_add "apt install portaudio19-dev"; fi
if [ "$NEED_PIP_PKG"        = "true" ]; then plan_add "apt install python3-pip"; fi
if [ "$NEED_ALSA_UTILS"     = "true" ]; then plan_add "apt install alsa-utils"; fi
if [ "$NEED_PY_DEPS"        = "true" ]; then plan_add "pip3 install -r requirements.txt (system-wide)"; fi
if [ "$NEED_RECORDINGS_DIR" = "true" ]; then plan_add "mkdir recordings/"; fi
plan_add "rewrite + restart systemd service '$SERVICE_NAME'"

echo ""
read -r -p "  Proceed? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
[[ ! "$CONFIRM" =~ ^[Yy]$ ]] && { echo "  Aborted."; exit 0; }

# =============================================================================
# INSTALL PHASE
# =============================================================================
section "INSTALL PHASE"

# ── apt ───────────────────────────────────────────────────────────────────────
if [ "$NEED_APT_UPDATE" = "true" ]; then
    info "apt update..."
    sudo apt update -qq
    ok "apt updated"
fi

APT_PKGS=()
[ "$NEED_PORTAUDIO"  = "true" ] && APT_PKGS+=("portaudio19-dev")
[ "$NEED_PIP_PKG"    = "true" ] && APT_PKGS+=("python3-pip")
[ "$NEED_ALSA_UTILS" = "true" ] && APT_PKGS+=("alsa-utils")

if [ ${#APT_PKGS[@]} -gt 0 ]; then
    info "Installing: ${APT_PKGS[*]}"
    sudo apt install -y -qq "${APT_PKGS[@]}"
    for pkg in "${APT_PKGS[@]}"; do ok "Installed: $pkg"; INSTALLED+=("$pkg"); done
fi

# ── Remove eventlet if present ────────────────────────────────────────────────
if python3 -c "import eventlet" &>/dev/null; then
    info "Removing eventlet..."
    pip3 install --break-system-packages --quiet pip --upgrade 2>/dev/null || true
    pip3 uninstall -y eventlet 2>/dev/null || \
        sudo pip3 uninstall -y eventlet 2>/dev/null || true
    ok "eventlet removed"; INSTALLED+=("removed-eventlet")
fi

# ── Python packages (system-wide via sudo) ────────────────────────────────────
if [ "$NEED_PY_DEPS" = "true" ]; then
    info "Installing Python packages system-wide (sudo pip3)..."
    sudo pip3 install --break-system-packages -r "$PROJECT_DIR/requirements.txt"
    ok "Python packages installed"; INSTALLED+=("python-packages")
fi

# ── recordings/ ──────────────────────────────────────────────────────────────
if [ "$NEED_RECORDINGS_DIR" = "true" ]; then
    mkdir -p "$PROJECT_DIR/recordings"
    ok "Created recordings/"; INSTALLED+=("recordings-dir")
fi

# ── systemd service ───────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
info "Writing $SERVICE_FILE ..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Pi Audio Recorder (pirecoder)
After=network.target sound.target local-fs.target
Wants=sound.target

[Service]
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStartPre=/bin/sleep 8
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/home/${SERVICE_USER}
SupplementaryGroups=audio

[Install]
WantedBy=multi-user.target
EOF
ok "Service file written  (ExecStart: $PYTHON_BIN $PROJECT_DIR/app.py)"

sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# ── Poll up to 20 s for active / failed ──────────────────────────────────────
info "Waiting for service to start..."
SVC_FINAL="unknown"
for i in $(seq 1 20); do
    sleep 1
    SVC_FINAL=$(sudo systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
    if [ "$SVC_FINAL" = "active" ]; then
        ok "Service is active and enabled on boot"; INSTALLED+=("systemd-service"); break
    elif [ "$SVC_FINAL" = "failed" ]; then break; fi
    printf "  ${CYAN}→${RESET}  still starting... (%ss)\r" "$i"
done

if [ "$SVC_FINAL" != "active" ]; then
    echo ""
    warn "Service status: $SVC_FINAL"
    warn "Check: journalctl -u $SERVICE_NAME -n 30 --no-pager"
    WARNINGS+=("Service $SVC_FINAL — check journalctl")
fi

# ── Post-install mic recheck ──────────────────────────────────────────────────
if command -v arecord &>/dev/null; then
    arecord -l 2>/dev/null | grep -q "card" || \
        WARNINGS+=("No mic — plug in USB mic then: sudo systemctl restart $SERVICE_NAME")
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
    for item in "${INSTALLED[@]}"; do echo -e "    ${GREEN}✔${RESET}  $item"; done
    echo ""
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
    echo -e "  ${BOLD}Already present (skipped):${RESET}"
    for item in "${SKIPPED[@]}"; do echo -e "    ${DIM}–  $item${RESET}"; done
    echo ""
fi

if [ ${#WARNINGS[@]} -gt 0 ]; then
    echo -e "  ${BOLD}${YELLOW}Warnings:${RESET}"
    for w in "${WARNINGS[@]}"; do echo -e "    ${YELLOW}⚠${RESET}  $w"; done
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
echo -e "    ${DIM}journalctl -u $SERVICE_NAME -n 30 --no-pager${RESET}"
echo ""
