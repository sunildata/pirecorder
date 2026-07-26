# Raspberry Pi OS Setup and SD Card Imaging

## Part 1 — Fresh install

### 1. Flash the OS

Use **Raspberry Pi Imager**. Choose **Raspberry Pi OS Lite (64-bit)** — the
desktop environment wastes RAM and CPU that recording can use.

Before writing, open the gear icon and pre-configure:

| Setting | Value |
|---|---|
| Hostname | `zoompi` |
| Enable SSH | Yes, password authentication |
| Username | `pi` (the installer adapts to any name) |
| Wi-Fi SSID / password | Your network |
| Wi-Fi country | Required, or the radio stays disabled |
| Locale / timezone | Yours — this determines recording filenames |

### 2. First boot

```bash
ssh pi@zoompi.local        # or ssh pi@<ip>
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### 3. Tune the OS for continuous recording

These are not optional if you want 12-hour reliability.

```bash
# Shrink the GPU allocation — headless needs almost none, and this frees
# ~48 MB of RAM for buffers.
echo "gpu_mem=16" | sudo tee -a /boot/firmware/config.txt

# Disable Wi-Fi power management. Without this the radio sleeps and the
# phone's connection stalls; recording is unaffected but the UI appears dead.
sudo tee /etc/systemd/system/wifi-powersave-off.service >/dev/null <<'EOF'
[Unit]
Description=Disable Wi-Fi power saving
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/iw dev wlan0 set power_save off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now wifi-powersave-off

# Journald caps: unbounded logs will eventually fill the card.
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=100M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

# Drop services that serve no purpose on a recorder.
sudo systemctl disable --now bluetooth hciuart triggerhappy avahi-daemon 2>/dev/null
```

Reduce SD card wear — writes are what kill cards, and a recorder does a lot
of them:

```bash
# Mount /tmp and logs in RAM
sudo tee -a /etc/fstab >/dev/null <<'EOF'
tmpfs /tmp     tmpfs defaults,noatime,nosuid,size=100m 0 0
tmpfs /var/log tmpfs defaults,noatime,nosuid,size=50m  0 0
EOF

# noatime on the root filesystem avoids a metadata write on every read
sudo sed -i 's|\(\s/\s\+ext4\s\+defaults\)|\1,noatime|' /etc/fstab
```

Disable swap. Swapping during recording causes audio glitches, and on an SD
card it is destructive:

```bash
sudo dphys-swapfile swapoff
sudo systemctl disable dphys-swapfile
```

Reboot to apply everything:

```bash
sudo reboot
```

### 4. Verify the audio interface

Plug in the USB interface, then:

```bash
arecord -l
```

You want something like:

```
card 1: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
```

Record a 5-second test:

```bash
arecord -D hw:1,0 -f S16_LE -r 48000 -c 2 -d 5 /tmp/test.wav
aplay /tmp/test.wav          # if you have headphones on the Pi
ls -la /tmp/test.wav         # ~960 KB for 5 s stereo 48 kHz 16-bit
```

If `arecord -l` shows nothing, try a different USB port or a powered hub.

### 5. Install ZoomPi

```bash
git clone <your-repo-url> ~/zoompi
cd ~/zoompi
bash install.sh              # add --hardware for GPIO/OLED support
```

The installer checks what is already present, installs only what is missing,
writes the systemd units, and then verifies by polling the real HTTP endpoint
rather than trusting `systemctl`'s idea of "active".

When it finishes:

```
Open on your phone:   http://192.168.1.19:5000
Default password:     zoompi
```

**Change the password immediately** in Settings.

---

## Part 2 — Creating a distributable SD image

Once one card is configured the way you want, clone it so you can deploy
identical recorders.

### Prepare the source card

Remove anything machine-specific before imaging:

```bash
cd ~/zoompi
sudo systemctl stop zoompi

# Recordings, database, logs, and the session secret must not ship
rm -rf recordings/* data/zoompi.db* data/logs/* data/secret.key data/config.json

# Saved Wi-Fi credentials
sudo rm -f /etc/NetworkManager/system-connections/*.nmconnection

# SSH host keys — otherwise every clone shares an identity
sudo rm -f /etc/ssh/ssh_host_*
sudo systemctl enable regenerate_ssh_host_keys 2>/dev/null || true

# Shell history and logs
history -c && rm -f ~/.bash_history
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s

# Zero free space so the image compresses well
sudo dd if=/dev/zero of=/zero.fill bs=1M 2>/dev/null; sudo rm -f /zero.fill
sync
sudo shutdown -h now
```

### Capture the image

**Linux / macOS:**

```bash
diskutil list                       # macOS: identify the card
lsblk                               # Linux

sudo dd if=/dev/sdX of=zoompi.img bs=4M status=progress conv=fsync
```

**Windows:** use Win32 Disk Imager's "Read" function, or Raspberry Pi
Imager's backup feature.

### Shrink it

A raw image is the full card size. `PiShrink` cuts it to the used space and
makes it auto-expand on first boot:

```bash
wget https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
chmod +x pishrink.sh
sudo ./pishrink.sh -Z zoompi.img zoompi-v1.0.img
```

`-Z` gzips the result. A trimmed Lite install typically lands around 1.2 GB
compressed.

### Verify before distributing

Flash the image to a *different* card and confirm:

- [ ] It boots and the filesystem expanded (`df -h`)
- [ ] `systemctl status zoompi` is active
- [ ] The web interface loads
- [ ] SSH host keys were regenerated (a new fingerprint on first connect)
- [ ] No recordings from the source card
- [ ] Settings are back at defaults
- [ ] A test recording works end to end

### First-boot instructions for recipients

```
1. Flash zoompi-v1.0.img.gz with Raspberry Pi Imager
2. Insert the card, connect the USB audio interface, power on
3. Wait ~90 seconds for first boot and filesystem expansion
4. Connect your phone to the "ZoomPi" Wi-Fi network
   Password: zoompi12345
5. Browse to http://10.42.0.1:5000
6. Log in with: zoompi
7. Change both passwords in Settings
8. Optionally add your venue Wi-Fi in Settings → Wi-Fi
```

---

## Part 3 — Operating without any network

The recorder does not need a network at all. With `wifi_mode` set to `auto`
and no known network in range, it hosts its own AP automatically at boot.

If you want it to record with no phone involved whatsoever, wire the GPIO
buttons (see `docs/HARDWARE.md`) and set `hardware_enabled`. Press Record,
press Stop, pull the card. The LED and OLED tell you what is happening.

---

## Troubleshooting

### Service will not start

```bash
sudo systemctl status zoompi
sudo journalctl -u zoompi -n 50 --no-pager

# Run it in the foreground, where errors are obvious
sudo systemctl stop zoompi
python3 ~/zoompi/run.py
```

### "Address already in use" / port 5000 taken

Almost always an older installation still running. Deleting the previous
`app.py` during a `git pull` does **not** stop the process already running it
— Linux keeps a running program alive after its file is removed, so the old
service holds port 5000 and the new one cannot bind. systemd then restarts
the new service in a loop, and every attempt fails the same way.

`install.sh` now detects and retires previous installations automatically.
To resolve it by hand:

```bash
# What is holding the port?
sudo ss -tlnp | grep :5000

# If it is an old service, disable it permanently
sudo systemctl disable --now pirecorder.service

# If it is a stray manual run, kill the PID
sudo kill <pid>

sudo systemctl restart zoompi
curl -s localhost:5000/api/health
```

### `ERR_CONNECTION_REFUSED` from the phone

```bash
# Is it listening?
ss -tlnp | grep 5000

# Is the IP what you think it is?
hostname -I

# Firewall?
sudo ufw status && sudo ufw allow 5000/tcp
```

The unit has an intentional 8-second `ExecStartPre` delay so USB audio can
enumerate — give it 15 seconds after boot before concluding anything is wrong.

### No capture device

```bash
lsusb                      # is the interface even enumerated?
arecord -l
dmesg | tail -30           # USB errors, power problems
groups                     # 'pi' must include 'audio'
```

If the group is missing, `sudo usermod -aG audio pi` and **reboot** — group
changes do not apply to a running session.

### Recording starts then immediately stops

Almost always ALSA rejecting the requested format:

```bash
sudo journalctl -u zoompi | grep -i arecord
arecord -D hw:1,0 -f S24_3LE -r 48000 -c 2 -d 1 /tmp/x.wav
```

If that fails but `S16_LE` works, your interface is 16-bit. The Settings page
should already reflect this from its capability probe.

### Audio dropouts

In order of likelihood:

1. **Under-voltage.** Check the dashboard warning and `vcgencmd get_throttled`.
   A non-zero result means your power supply is inadequate.
2. **Slow SD card.** Test with
   `dd if=/dev/zero of=~/t bs=1M count=500 conv=fsync` — you want 10 MB/s or
   better.
3. **USB contention.** Move the interface off a hub, or off a port shared with
   other devices.
4. **Thermal throttling.** Above ~80 °C. Add airflow.

### Web interface is unreachable but recording continues

This is the system working as designed. Recording does not depend on the
network. Fix the Wi-Fi at your leisure:

```bash
nmcli device wifi list
sudo nmcli device wifi connect "SSID" password "..."
```

Your audio is safe on the card the entire time.
