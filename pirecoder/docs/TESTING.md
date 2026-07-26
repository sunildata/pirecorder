# Testing Guide

Before trusting this with an event you cannot re-run, work through the
reliability tests. They are the ones that matter — the rest is convenience.

## Automated suite

```bash
python3 tests/smoke_test.py
```

67 checks covering the level analyser, WAV header repair, config persistence
and corruption recovery, password hashing, path-traversal defence, the
database layer, and every HTTP route including auth enforcement. Runs in a
scratch directory and needs no audio hardware.

Expected: `67 passed, 0 failed`.

---

## Reliability tests

These simulate the failures the design exists to survive. Run each at least
once on the actual hardware you will use.

### 1. Phone disconnects mid-recording

The core requirement.

1. Start recording from your phone.
2. Turn on airplane mode, or walk out of Wi-Fi range.
3. Wait 5 minutes.
4. Reconnect.

**Expected:** the dashboard reconnects on its own and shows the timer still
running with the correct elapsed time. The file grew the whole time.

```bash
ls -la recordings/$(date +%F)/     # size should reflect the full duration
```

### 2. Browser closed entirely

1. Start recording.
2. Force-quit the browser.
3. Wait 2 minutes, reopen, log in.

**Expected:** recording still running, timer accurate.

### 3. Wi-Fi router powered off

1. Start recording.
2. Unplug the router for 3 minutes.
3. Restore power.

**Expected:** recording never stopped. If `wifi_mode` is `auto`, the Pi may
have fallen back to hosting its own hotspot — check with `nmcli device wifi`.

### 4. Power loss (the important one)

1. Start recording.
2. Let it run for at least 2 minutes.
3. **Pull the power cord.** Not a clean shutdown — actually yank it.
4. Restore power and wait for boot.

**Expected:**

```bash
# The journal was cleared, meaning recovery ran
ls -la recordings/$(date +%F)/.*.journal.json     # should not exist

# The file plays and reports a correct duration
ffprobe recordings/$(date +%F)/*.wav 2>&1 | grep Duration

# Recovery was logged
journalctl -u zoompi | grep -i recover
```

You should lose only the last second or so. If the file will not open in a
player, recovery failed — file that as a bug.

### 5. Storage exhaustion

```bash
# Fill the card, leaving slightly less than min_free_mb
df -h recordings/
fallocate -l <size>G recordings/ballast
```

Start recording and wait.

**Expected:** recording stops cleanly with a `storage_full` event, the
dashboard shows "Storage full", and the last file is valid and playable —
not truncated mid-header.

```bash
rm recordings/ballast
```

### 6. USB interface unplugged mid-take

1. Start recording.
2. Unplug the audio interface.
3. Wait 15 seconds, plug it back in.

**Expected:** the watchdog notices the dead capture within ~2 seconds,
repairs the partial file, and starts a new segment once the device returns.
The dashboard shows "Capture hiccup — recording resumed automatically".

```bash
journalctl -u zoompi | grep capture_restarted
```

Audio during the unplugged window is genuinely gone — nothing can prevent
that — but everything before and after is intact and playable.

---

## Endurance test

Run this before any event longer than a couple of hours.

```bash
curl -s -c jar -X POST http://localhost:5000/api/login \
     -H 'Content-Type: application/json' -d '{"password":"zoompi"}'
curl -s -b jar -X POST http://localhost:5000/api/record/start \
     -H 'Content-Type: application/json' -d '{"label":"endurance"}'

# Sample resources every minute
while true; do
  echo "$(date +%T) $(curl -s -b jar localhost:5000/api/system \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); \
      print(f"cpu={d[\"cpu_percent\"]}% mem={d[\"memory\"][\"percent\"]}% \
temp={d[\"cpu_temp_c\"]}C free={d[\"storage\"][\"free_mb\"]}MB")')"
  sleep 60
done | tee endurance.log
```

Let it run 12+ hours, then verify:

| Check | Target |
|---|---|
| CPU | under 20% sustained |
| Memory | under 300 MB, and **flat** — a slow climb is a leak |
| Temperature | under 75 °C |
| Under-voltage flags | none |
| Files | continuous, each playable |

```bash
# No gaps: total audio duration should match wall-clock elapsed time
for f in recordings/$(date +%F)/*.wav; do
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$f"
done | awk '{s+=$1} END {printf "total: %.1f h\n", s/3600}'

# Every file decodes without error
for f in recordings/$(date +%F)/*.wav; do
  ffmpeg -v error -i "$f" -f null - 2>&1 | grep -q . \
    && echo "CORRUPT: $f" || echo "ok: $f"
done
```

---

## Audio quality

### Verify the recorded format matches what you asked for

```bash
ffprobe -v error -show_streams recordings/$(date +%F)/*.wav \
  | grep -E 'sample_rate|channels|bits_per_sample'
```

If this shows 16-bit when you selected 24, your interface does not support
24-bit — see `docs/HARDWARE.md`. The Settings page should already have told
you this.

### Check for dropped samples

```bash
# Duration from the header should match duration from the byte count
python3 - <<'EOF'
import struct, sys, glob
for path in glob.glob('recordings/*/*.wav'):
    with open(path,'rb') as f: h = f.read(44)
    ch, rate = struct.unpack('<HI', h[22:28])
    depth = struct.unpack('<H', h[34:36])[0]
    declared = struct.unpack('<I', h[40:44])[0]
    import os; actual = os.path.getsize(path) - 44
    flag = 'OK ' if declared == actual else 'MISMATCH'
    print(f"{flag} {path}: header={declared} actual={actual}")
EOF
```

A mismatch means the header was never patched — run the service once to
trigger recovery, or the file was still being written.

### Level calibration

1. Send a steady 1 kHz tone from the mixer at its nominal output.
2. Watch the meters — they should read around −18 dBFS for a 0 VU tone.
3. Push the mixer to its clip point; ZoomPi's clip indicator should latch.

If the meters never approach 0 dBFS even at full mixer output, your interface
input gain is too low. If they clip while the mixer shows healthy levels, the
input is too hot for a consumer line input — pad it or use a proper interface.

---

## Web interface

Test on the phone you will actually use.

| Check | Expectation |
|---|---|
| Login | Rejects a wrong password, accepts the right one |
| Record / Pause / Resume / Stop | Timer and state track correctly |
| Recording lock | Stop asks for confirmation when enabled |
| Markers | Appear as chips with the right offsets |
| Meters | Respond to audio within ~100 ms; clip latches until reset |
| Screen off, then on | State is correct immediately, not after a delay |
| Files: play | Streams and seeks without downloading the whole file |
| Files: rename | Rejects `../` and odd characters |
| Files: bulk ZIP | Downloads and extracts correctly |
| Settings | Only offers formats the interface supports |
| Settings during a take | Audio format fields are disabled |

### Multiple simultaneous clients

Open the dashboard on two phones and a laptop. All three should show
identical state, and an action on one should appear on the others within a
second.

---

## Wi-Fi modes

```bash
# Saved network
curl -s -b jar -X POST localhost:5000/api/wifi/connect \
  -H 'Content-Type: application/json' \
  -d '{"ssid":"VenueWiFi","password":"...","priority":10}'

# Phone hotspot — mark it so it is tried after real networks
curl -s -b jar -X POST localhost:5000/api/wifi/connect \
  -H 'Content-Type: application/json' \
  -d '{"ssid":"MyPhone","password":"...","is_hotspot":true}'

# Full fallback chain
curl -s -b jar -X POST localhost:5000/api/wifi/auto
```

**Fallback test:** save a network, power off its router, reboot the Pi. Within
about a minute the Pi should be hosting `ZoomPi`. Connect your phone to it and
browse to `http://10.42.0.1:5000`.

**Critical:** run `POST /api/wifi/auto` *while recording* and confirm the take
is unaffected. Network changes must never touch audio.

---

## Health monitoring

```bash
systemctl status zoompi zoompi-health.timer
systemctl list-timers zoompi-health.timer
curl -s localhost:5000/api/health

# Simulate a wedged process — the timer should restart it within 2 minutes
sudo kill -STOP $(pgrep -f 'python3.*run.py')
sleep 150
systemctl status zoompi        # should have restarted
```

---

## Pre-event checklist

Print this.

- [ ] `python3 tests/smoke_test.py` passes
- [ ] Power-loss test done on this exact hardware
- [ ] High-endurance SD card, and enough free space for the full event **plus 50%**
- [ ] `df -h` confirms the space
- [ ] Power supply is 2.5 A+; no under-voltage warning on the dashboard
- [ ] USB audio cable strain-relieved
- [ ] Levels calibrated against the mixer, peaks around −12 dBFS
- [ ] `recording_lock` enabled if the device is unattended
- [ ] Wi-Fi plan decided (venue network vs. own hotspot) and tested on site
- [ ] Password changed from the default
- [ ] A 10-minute full-chain rehearsal recorded and played back
- [ ] `journalctl -u zoompi -p warning` is clean
- [ ] Auto-split configured (2 GB is a sensible default)
- [ ] If it truly cannot be lost: a second recorder running in parallel

That last point is not a cop-out. Professional practice for irreplaceable
audio is redundant capture, and no amount of software engineering substitutes
for a second device.
