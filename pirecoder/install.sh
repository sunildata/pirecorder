#!/usr/bin/env bash
#
# ZoomPi installer — idempotent. Detects what is already present, installs
# only what is missing, and verifies the result by probing the running API.
#
#   bash install.sh              normal install / upgrade
#   bash install.sh --hardware   also install GPIO + OLED support
#   bash install.sh --uninstall  remove services (recordings are kept)
#
set -uo pipefail

readonly INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SERVICE_USER="${SUDO_USER:-$USER}"
readonly RECORDINGS_DIR="${INSTALL_DIR}/recordings"
readonly SERVICE_NAME="zoompi"
readonly PORT=5000

WITH_HARDWARE=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --hardware)  WITH_HARDWARE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# ── Output helpers ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
  R='\033[0;31m'; G='\033[0;32m'; Y='\033[0;33m'; B='\033[0;34m'; D='\033[2m'; N='\033[0m'
else
  R=''; G=''; Y=''; B=''; D=''; N=''
fi
step() { printf "\n${B}==>${N} %s\n" "$1"; }
ok()   { printf "  ${G}ok${N}   %s\n" "$1"; }
skip() { printf "  ${D}skip${N} %s\n" "$1"; }
warn() { printf "  ${Y}warn${N} %s\n" "$1"; }
die()  { printf "  ${R}fail${N} %s\n" "$1"; exit 1; }

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing ZoomPi services"
  sudo systemctl disable --now "${SERVICE_NAME}.service"      2>/dev/null && ok "service stopped"
  sudo systemctl disable --now "${SERVICE_NAME}-health.timer" 2>/dev/null && ok "health timer stopped"
  sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service" \
             "/etc/systemd/system/${SERVICE_NAME}-health.service" \
             "/etc/systemd/system/${SERVICE_NAME}-health.timer" \
             "/etc/polkit-1/rules.d/50-zoompi-network.rules" \
             "/etc/sudoers.d/zoompi-nmcli"
  sudo nmcli connection delete zoompi-ap 2>/dev/null && ok "hotspot profile removed"
  sudo systemctl daemon-reload
  ok "services removed — recordings in ${RECORDINGS_DIR} were kept"
  exit 0
fi

echo "╔══════════════════════════════════════════════════════╗"
echo "║   ZoomPi — Wireless Audio Recorder Installer         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo "  install dir : ${INSTALL_DIR}"
echo "  service user: ${SERVICE_USER}"

# ── 1. Sanity ────────────────────────────────────────────────────────────────
step "Checking environment"

[ -f "${INSTALL_DIR}/run.py" ] || die "run.py not found — run this from the project directory"

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  die "Run as a normal user (the script calls sudo itself), not as root"
fi

if ! sudo -n true 2>/dev/null; then
  echo "  This installer needs sudo; you may be prompted for your password."
  sudo true || die "sudo required"
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
  || die "python3 not found"
ok "python ${PY_VERSION}"

if grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  ok "$(tr -d '\0' < /proc/device-tree/model)"
else
  warn "not a Raspberry Pi — hardware features will be unavailable"
fi

# ── 2. System packages ───────────────────────────────────────────────────────
step "System packages"

APT_NEEDED=()
for pkg in alsa-utils python3-pip python3-flask curl; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    skip "$pkg"
  else
    APT_NEEDED+=("$pkg")
  fi
done

# ffmpeg is only required for MP3 export and post-processing.
if command -v ffmpeg >/dev/null 2>&1; then
  skip "ffmpeg"
else
  APT_NEEDED+=(ffmpeg)
fi

if [ "${#APT_NEEDED[@]}" -gt 0 ]; then
  echo "  installing: ${APT_NEEDED[*]}"
  sudo apt-get update -qq || warn "apt update failed — continuing with cached lists"
  sudo apt-get install -y -qq "${APT_NEEDED[@]}" || die "apt install failed"
  ok "installed ${#APT_NEEDED[@]} package(s)"
fi

# ── 3. Python packages ───────────────────────────────────────────────────────
step "Python packages"

pip_install() {
  # Bookworm marks the system Python as externally managed; this project is
  # intentionally installed system-wide so systemd can run it without a venv.
  sudo pip3 install --quiet --break-system-packages "$@" 2>/dev/null \
    || sudo pip3 install --quiet "$@"
}

PIP_NEEDED=()
python3 -c "import flask"          2>/dev/null || PIP_NEEDED+=("Flask==3.0.3")
python3 -c "import flask_socketio" 2>/dev/null || PIP_NEEDED+=("flask-socketio==5.3.6")
python3 -c "import simple_websocket" 2>/dev/null || PIP_NEEDED+=("simple-websocket==1.0.0")

if [ "${#PIP_NEEDED[@]}" -gt 0 ]; then
  echo "  installing: ${PIP_NEEDED[*]}"
  pip_install "${PIP_NEEDED[@]}" || die "pip install failed"
  ok "installed ${#PIP_NEEDED[@]} package(s)"
else
  skip "flask, flask-socketio, simple-websocket"
fi

# eventlet is incompatible with Python 3.12+ and breaks Flask-SocketIO's
# threading mode if it happens to be importable.
if python3 -c "import eventlet" 2>/dev/null; then
  warn "removing eventlet (incompatible with Python ${PY_VERSION})"
  sudo pip3 uninstall -y -q eventlet --break-system-packages 2>/dev/null \
    || sudo pip3 uninstall -y -q eventlet 2>/dev/null || true
fi

if [ "$WITH_HARDWARE" -eq 1 ]; then
  step "Hardware support"
  HW_NEEDED=()
  python3 -c "import gpiozero" 2>/dev/null || HW_NEEDED+=("gpiozero==2.0.1" "lgpio==0.2.2.0")
  python3 -c "import adafruit_ssd1306" 2>/dev/null || \
    HW_NEEDED+=("adafruit-circuitpython-ssd1306==2.12.16" "Pillow==10.4.0")
  if [ "${#HW_NEEDED[@]}" -gt 0 ]; then
    pip_install "${HW_NEEDED[@]}" && ok "GPIO/OLED libraries installed" \
      || warn "hardware libraries failed — buttons and display will be disabled"
  else
    skip "gpiozero, adafruit-ssd1306"
  fi
  sudo raspi-config nonint do_i2c 0 2>/dev/null && ok "I2C enabled" || warn "could not enable I2C"
fi

# ── 4. Audio device ──────────────────────────────────────────────────────────
step "Audio capture device"

if ! id -nG "$SERVICE_USER" | grep -qw audio; then
  sudo usermod -aG audio "$SERVICE_USER" && ok "added ${SERVICE_USER} to the audio group"
  warn "group change applies after reboot"
else
  skip "${SERVICE_USER} already in audio group"
fi

CAPTURE_LIST="$(arecord -l 2>/dev/null)"
if echo "$CAPTURE_LIST" | grep -q '^card'; then
  echo "$CAPTURE_LIST" | grep '^card' | sed 's/^/  /'
  if echo "$CAPTURE_LIST" | grep -qi usb; then
    ok "USB audio interface detected"
  else
    warn "no USB interface found — the Pi has no built-in line input"
  fi
else
  warn "no capture device detected — plug in your USB interface before recording"
fi

# ── 5. Directories ───────────────────────────────────────────────────────────
step "Directories"

for dir in "${RECORDINGS_DIR}" "${INSTALL_DIR}/data" "${INSTALL_DIR}/data/logs"; do
  if [ -d "$dir" ]; then
    skip "$(basename "$dir")/"
  else
    mkdir -p "$dir" && ok "created $(basename "$dir")/"
  fi
done
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" \
  "${RECORDINGS_DIR}" "${INSTALL_DIR}/data" 2>/dev/null || true

FREE_GB="$(df -BG --output=avail "${RECORDINGS_DIR}" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "$FREE_GB" ]; then
  HOURS=$(( FREE_GB * 1024 / 660 ))   # ~660 MB per hour at 48 kHz/16-bit stereo
  ok "${FREE_GB} GB free (~${HOURS} h of stereo WAV)"
  [ "$FREE_GB" -lt 2 ] && warn "very low free space"
fi

# ── 5b. Network permissions ──────────────────────────────────────────────────
# Without this the service can read Wi-Fi state but cannot change it, so the
# fallback access point never starts when no known network is in range.
step "Network permissions"

if command -v nmcli >/dev/null 2>&1; then
  if id -nG "$SERVICE_USER" | grep -qw netdev; then
    skip "${SERVICE_USER} already in netdev group"
  else
    sudo usermod -aG netdev "$SERVICE_USER" && ok "added ${SERVICE_USER} to netdev"
  fi

  POLKIT_DIR=/etc/polkit-1/rules.d
  if [ -d "$POLKIT_DIR" ]; then
    sed "s|__USER__|${SERVICE_USER}|g" \
        "${INSTALL_DIR}/systemd/50-zoompi-network.rules" \
      | sudo tee "${POLKIT_DIR}/50-zoompi-network.rules" >/dev/null
    sudo chmod 644 "${POLKIT_DIR}/50-zoompi-network.rules"
    ok "polkit rule installed"
    sudo systemctl restart polkit 2>/dev/null || true
  else
    warn "polkit rules directory not found — falling back to sudoers"
  fi

  # Belt and braces: the code retries through sudo if polkit still refuses.
  echo "${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/nmcli" \
    | sudo tee /etc/sudoers.d/zoompi-nmcli >/dev/null
  sudo chmod 440 /etc/sudoers.d/zoompi-nmcli
  if sudo visudo -cf /etc/sudoers.d/zoompi-nmcli >/dev/null 2>&1; then
    ok "sudoers fallback installed"
  else
    sudo rm -f /etc/sudoers.d/zoompi-nmcli
    warn "sudoers entry rejected — removed"
  fi
else
  warn "nmcli not found — Wi-Fi management will be unavailable"
fi

# ── 6. systemd ───────────────────────────────────────────────────────────────
step "systemd services"

render_unit() {
  sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
      -e "s|__USER__|${SERVICE_USER}|g" \
      -e "s|__RECORDINGS_DIR__|${RECORDINGS_DIR}|g" \
      "$1"
}

# Always rewrite: paths or the service user may have changed since last run.
render_unit "${INSTALL_DIR}/systemd/zoompi.service" \
  | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
ok "${SERVICE_NAME}.service"

render_unit "${INSTALL_DIR}/systemd/zoompi-health.service" \
  | sudo tee "/etc/systemd/system/${SERVICE_NAME}-health.service" >/dev/null
sudo cp "${INSTALL_DIR}/systemd/zoompi-health.timer" \
        "/etc/systemd/system/${SERVICE_NAME}-health.timer"
ok "${SERVICE_NAME}-health.timer"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1 && ok "enabled at boot"
sudo systemctl enable "${SERVICE_NAME}-health.timer" >/dev/null 2>&1 || true

# ── 6b. Retire earlier installations ─────────────────────────────────────────
# Upgrading from the pre-ZoomPi prototype leaves its service running. Deleting
# the old app.py during `git pull` does not stop the process that is already
# running it, so it keeps holding port 5000 and the new service cannot bind.
step "Checking for a previous installation"

port_holder_pid() {
  sudo ss -tlnpH "sport = :${PORT}" 2>/dev/null \
    | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2
}

unit_of_pid() {
  # The cgroup path is the reliable way to map a PID back to its unit.
  grep -o '[a-zA-Z0-9@_.\-]*\.service' "/proc/$1/cgroup" 2>/dev/null | head -1
}

RETIRED=0
for legacy in pirecorder audiorecorder recorder piaudio; do
  if systemctl list-unit-files "${legacy}.service" 2>/dev/null | grep -q "${legacy}.service"; then
    sudo systemctl disable --now "${legacy}.service" >/dev/null 2>&1
    ok "retired legacy ${legacy}.service"
    RETIRED=1
  fi
done

# Whatever the old unit was called, something may still hold the port.
HOLDER_PID="$(port_holder_pid)"
if [ -n "${HOLDER_PID:-}" ]; then
  HOLDER_UNIT="$(unit_of_pid "$HOLDER_PID")"
  HOLDER_CMD="$(ps -p "$HOLDER_PID" -o args= 2>/dev/null | cut -c1-70)"

  if [ "${HOLDER_UNIT}" = "${SERVICE_NAME}.service" ]; then
    skip "port ${PORT} held by our own service (will restart)"
  elif [ -n "$HOLDER_UNIT" ]; then
    warn "port ${PORT} held by ${HOLDER_UNIT} — disabling it"
    sudo systemctl disable --now "$HOLDER_UNIT" >/dev/null 2>&1
    ok "retired ${HOLDER_UNIT}"
    RETIRED=1
  else
    # Not under systemd — most likely a manual `python3 app.py` left running.
    warn "port ${PORT} held by PID ${HOLDER_PID}: ${HOLDER_CMD}"
    sudo kill "$HOLDER_PID" 2>/dev/null
    sleep 2
    kill -0 "$HOLDER_PID" 2>/dev/null && sudo kill -9 "$HOLDER_PID" 2>/dev/null
    ok "stopped stray process ${HOLDER_PID}"
    RETIRED=1
  fi

  # Give the kernel a moment to release the socket.
  for _ in $(seq 1 10); do
    [ -z "$(port_holder_pid)" ] && break
    sleep 1
  done
fi

[ "$RETIRED" -eq 0 ] && skip "no previous installation found"

step "Starting service"
sudo systemctl restart "${SERVICE_NAME}.service"
sudo systemctl start "${SERVICE_NAME}-health.timer" 2>/dev/null || true

# ── 7. Verify ────────────────────────────────────────────────────────────────
# systemctl reporting "active" only means the process launched; the unit has
# an 8-second ExecStartPre, so poll the real endpoint instead.
step "Verifying"

printf "  waiting for the API to respond"
HEALTHY=0
for _ in $(seq 1 45); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  printf "."
  sleep 1
done
printf "\n"

if [ "$HEALTHY" -eq 1 ]; then
  ok "API responding on port ${PORT}"

  # Prove the service can actually change Wi-Fi, rather than assuming the
  # polkit rule took effect. A read-only nmcli call always succeeds, so this
  # checks a permission the fallback AP genuinely needs.
  if command -v nmcli >/dev/null 2>&1; then
    PERM="$(sudo -u "$SERVICE_USER" nmcli general permissions 2>/dev/null \
            | grep 'settings.modify.system' | awk '{print $2}')"
    case "$PERM" in
      yes)  ok "Wi-Fi management authorised for ${SERVICE_USER}" ;;
      auth) warn "Wi-Fi needs interactive auth — the sudo fallback will be used" ;;
      *)    warn "Wi-Fi management not authorised; hotspot fallback may fail"
            echo "    Check: sudo -u ${SERVICE_USER} nmcli general permissions" ;;
    esac
  fi
else
  printf "  ${R}fail${N} API did not respond within 45 s\n\n"

  # Name the actual cause instead of making the reader parse a log dump.
  RECENT="$(sudo journalctl -u "${SERVICE_NAME}.service" -n 40 --no-pager 2>/dev/null)"

  if echo "$RECENT" | grep -q "Address already in use"; then
    BLOCKER_PID="$(port_holder_pid)"
    echo "  Cause: something else is already listening on port ${PORT}."
    if [ -n "${BLOCKER_PID:-}" ]; then
      echo "  Held by PID ${BLOCKER_PID}: $(ps -p "$BLOCKER_PID" -o args= 2>/dev/null | cut -c1-60)"
      echo "  Unit:    $(unit_of_pid "$BLOCKER_PID" || echo 'not a systemd service')"
    fi
    echo
    echo "  Fix:  sudo kill ${BLOCKER_PID:-<pid>} && sudo systemctl restart ${SERVICE_NAME}"

  elif echo "$RECENT" | grep -qi "ModuleNotFoundError\|ImportError"; then
    echo "  Cause: a Python dependency is missing."
    echo "$RECENT" | grep -i "ModuleNotFoundError\|ImportError" | tail -3 | sed 's/^/    /'
    echo
    echo "  Fix:  sudo pip3 install --break-system-packages -r ${INSTALL_DIR}/requirements.txt"

  elif echo "$RECENT" | grep -q "Permission denied"; then
    echo "  Cause: a permissions problem."
    echo "  Fix:  sudo chown -R ${SERVICE_USER}: ${INSTALL_DIR}/data ${RECORDINGS_DIR}"

  else
    echo "  Recent logs:"
    echo "$RECENT" | tail -25 | sed 's/^/    /'
  fi

  echo
  echo "  Run it in the foreground for the full traceback:"
  echo "    sudo systemctl stop ${SERVICE_NAME} && python3 ${INSTALL_DIR}/run.py"
  exit 1
fi

# ── Done ─────────────────────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="<pi-ip>"

cat <<EOF

╔══════════════════════════════════════════════════════╗
║   Installation complete                              ║
╚══════════════════════════════════════════════════════╝

  Open on your phone:   http://${IP}:${PORT}
  Default password:     zoompi   (change it in Settings)

  systemctl status ${SERVICE_NAME}      service state
  journalctl -u ${SERVICE_NAME} -f      live logs
  bash install.sh --uninstall    remove services

EOF

if ! id -nG "$SERVICE_USER" | grep -qw audio; then
  warn "reboot once so the audio group membership takes effect"
fi
