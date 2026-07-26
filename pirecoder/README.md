# ZoomPi

A professional wireless audio recorder for Raspberry Pi, built for live
events, conferences, churches, weddings, and studio mixer feeds — situations
where losing the audio is not an option.

Control it from any phone browser. No app to install.

---

## The one thing that matters

**Audio never passes through Python.** `arecord` writes PCM straight from the
USB interface to the SD card. Python supervises, meters, and serves the web
interface, but a Python crash, a Wi-Fi drop, a closed browser, or a dead phone
battery cannot interrupt a recording.

A recording stops for exactly three reasons: you stop it, the power dies, or
the card fills. And if the power dies, you lose about a second — the rest is
already on disk and is automatically repaired on the next boot.

---

## Features

**Recording**
Stereo or mono WAV, up to the best format your interface supports (probed at
runtime, not assumed). Auto-split by size or duration. Markers and notes
during a take. Recording lock to prevent accidental stops. Optional −12 dB
dual-recording as clipping insurance. Optional MP3 export.

**Live control from any browser**
Transport, recording timer, true dBFS VU meters with peak-hold and latching
clip indicators, storage remaining, CPU temperature, battery, and IP —
updated ten times a second over WebSocket, with automatic fallback to polling
on a flaky network.

**Files**
Browse by day, search, sort, in-browser playback with seeking, rename,
delete, multi-select, and ZIP export.

**Networking**
Joins saved Wi-Fi networks by priority, then your phone's hotspot, and hosts
its own access point if neither is available. Changing networks never touches
audio.

**Hardware (optional)**
Record and stop buttons, a status LED, and an SSD1306 OLED. Entirely
additive — everything works without them.

**Security**
PBKDF2 password, session cookies, path-traversal defence on every file
operation. Fully offline; no cloud dependency of any kind.

---

## Install

On a Raspberry Pi running Raspberry Pi OS Lite (64-bit):

```bash
git clone <your-repo-url> ~/zoompi
cd ~/zoompi
bash install.sh
```

Then open `http://<pi-ip>:5000` on your phone. Default password: `zoompi` —
change it in Settings.

For GPIO buttons and an OLED:

```bash
bash install.sh --hardware
```

Full OS preparation, tuning, and SD-image instructions are in
[`docs/SETUP.md`](docs/SETUP.md).

---

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 3B or newer | |
| **High Endurance** microSD, 32–256 GB | A standard card will fail under continuous writes |
| USB audio interface with line input | Behringer UCA202 (16-bit) or UM2 / Scarlett Solo (24-bit) |
| 5 V **2.5 A+** power supply | Under-voltage is the top cause of USB audio dropouts |

Note: the UCA202/UCA222 is **16-bit/48 kHz only**. ZoomPi probes your
interface at startup and offers only the formats it genuinely supports, so
you always know what you are actually recording. See
[`docs/HARDWARE.md`](docs/HARDWARE.md).

---

## Project layout

```
zoompi/
├── recorder.py        arecord supervisor — the critical path
├── levels.py          VU metering by reading the file tail
├── audio_devices.py   Capability probing
├── storage.py         Library, search, export, cleanup
├── system.py          Telemetry
├── db.py              SQLite metadata (audio works without it)
├── wifi.py            AP / client mode chain
├── postprocess.py     Offline FFmpeg DSP + MP3
├── hardware.py        GPIO / OLED (optional)
├── auth.py            Password + sessions
├── api.py             REST surface
└── app.py             Factory + WebSocket broadcast

web/                   Templates and static assets
systemd/               Service units + health timer
docs/                  Architecture, hardware, API, setup, testing
tests/smoke_test.py    67 checks, no hardware needed
install.sh             Idempotent installer
run.py                 Entry point
```

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, reliability model, trade-offs, resource budget |
| [HARDWARE.md](docs/HARDWARE.md) | BOM, wiring diagrams, mixer connection, battery |
| [API.md](docs/API.md) | Every endpoint, WebSocket events, examples |
| [SETUP.md](docs/SETUP.md) | OS install, tuning, SD imaging, troubleshooting |
| [TESTING.md](docs/TESTING.md) | Reliability tests, endurance runs, pre-event checklist |

---

## Verify it works

```bash
python3 tests/smoke_test.py
```

Then run the reliability tests in [`docs/TESTING.md`](docs/TESTING.md) —
especially the power-loss test — on the exact hardware you plan to use.

---

## Known limitations

Stated plainly, because you should know them before an event:

- **The Pi 3's single radio cannot host an access point and stay joined to a
  network simultaneously.** Wi-Fi modes are a priority chain, not concurrent.
- **DSP is offline, not live.** A real-time limiter/compressor at 48 kHz on a
  Pi 3 cannot coexist with the "zero dropped samples" requirement, so
  processing runs after the take against a preserved master.
- **Audio during a USB unplug is gone.** The watchdog restarts capture within
  about two seconds and everything either side is intact, but nothing can
  recover samples from a disconnected device.
- **Live waveform display is not implemented.** Meters are; scrolling
  waveform rendering costs mobile battery and CPU for little practical gain
  over a good peak meter.
- **For genuinely irreplaceable audio, run a second recorder.** That is
  standard professional practice and no software design replaces it.
