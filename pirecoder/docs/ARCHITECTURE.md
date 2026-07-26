# ZoomPi Architecture

## The governing constraint

Everything below follows from one requirement: **losing audio is
unacceptable**. That single rule dictated most of the design, and where it
conflicted with a convenience feature, the feature lost.

The practical consequence is that **audio never passes through Python**.
`arecord` is handed the ALSA device and a filename, and it writes PCM
straight to the SD card. Python starts it, watches it, and reads metadata —
but a Python exception, a garbage-collection pause, a stalled HTTP request,
or the web server crashing outright cannot drop a single sample.

The previous prototype accumulated frames in a list and wrote the WAV on
stop. A 12-hour stereo take is roughly 8 GB, so that approach could not have
worked on a 1 GB Pi 3, and any interruption lost the entire session.

---

## System diagram

```
                        ┌──────────────────────┐
                        │   Phone / Laptop     │
                        │   (any browser)      │
                        └──────────┬───────────┘
                                   │ Wi-Fi
                    ┌──────────────┴───────────────┐
                    │   HTTP :5000  +  WebSocket   │
                    └──────────────┬───────────────┘
┌──────────────────────────────────┼──────────────────────────────────┐
│ Raspberry Pi 3                   │                                  │
│                    ┌─────────────▼─────────────┐                    │
│                    │  Flask + Flask-SocketIO   │                    │
│                    │  (threading async mode)   │                    │
│                    └─────────────┬─────────────┘                    │
│           ┌──────────────────────┼──────────────────────┐           │
│           │                      │                      │           │
│  ┌────────▼────────┐   ┌─────────▼────────┐   ┌─────────▼────────┐  │
│  │    Recorder     │   │   LevelMeter     │   │   Broadcaster    │  │
│  │  (supervisor)   │   │  (10 Hz poll)    │   │  (push to UI)    │  │
│  └────────┬────────┘   └─────────┬────────┘   └──────────────────┘  │
│           │ spawn / signal       │ pread tail                       │
│           │                      │                                  │
│  ┌────────▼──────────────────────▼────────┐                         │
│  │        arecord  (separate process)     │  ◄── the critical path  │
│  │        writes PCM directly to disk     │                         │
│  └────────────────────┬───────────────────┘                         │
│                       │                                             │
│  ┌────────────────────▼───────────────────┐                         │
│  │  /recordings/YYYY-MM-DD/*.wav          │                         │
│  │  + .journal.json  (crash marker)       │                         │
│  └────────────────────────────────────────┘                         │
│                                                                      │
│  Support services (none can block recording):                        │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌──────────┐            │
│  │  SQLite   │ │   Wi-Fi   │ │ PostProc   │ │  GPIO /  │            │
│  │ metadata  │ │ watchdog  │ │  (ffmpeg)  │ │   OLED   │            │
│  └───────────┘ └───────────┘ └────────────┘ └──────────┘            │
└──────────────────────────────────────────────────────────────────────┘
                       │                    │
              ┌────────▼───────┐   ┌────────▼────────┐
              │ USB Audio I/F  │   │  Buttons / LED  │
              │  (UCA202 etc.) │   │   (optional)    │
              └────────▲───────┘   └─────────────────┘
                       │
              ┌────────┴───────┐
              │  Mixer Line Out │
              └────────────────┘
```

---

## Module map

| Module | Responsibility | May block recording? |
|---|---|---|
| `recorder.py` | Spawns and supervises `arecord`, splits, journals, repairs | **It is the recording** |
| `levels.py` | Reads the growing file's tail for VU/peak | No — read-only |
| `audio_devices.py` | Enumerates cards, probes real capabilities | Only at start |
| `storage.py` | Listing, search, rename, delete, ZIP, cleanup | No |
| `system.py` | CPU temp, memory, disk, battery, throttle flags | No |
| `db.py` | SQLite metadata, markers, events, saved networks | No |
| `wifi.py` | AP / client mode chain, reconnect watchdog | No |
| `postprocess.py` | FFmpeg MP3 + DSP, deferred while recording | No — yields |
| `hardware.py` | GPIO buttons, status LED, OLED | No |
| `auth.py` | PBKDF2 password, session cookie | No |
| `api.py` | REST surface | No |
| `app.py` | Factory, WebSocket broadcast, startup recovery | No |

---

## How each reliability requirement is met

### "Never lose audio"

`arecord` owns the ALSA handle and the file descriptor. Python only sends it
signals. The watchdog thread polls every 2 seconds; if the process died on
its own — the one failure mode that silently loses audio — it repairs the
partial file and immediately opens a new segment, logging a `capture_restarted`
event. The gap is bounded by the poll interval rather than by whenever
someone notices.

### "Never pause because the phone disconnects"

The recorder holds no reference to any network object. There is no session
affinity, no client heartbeat, no "keep recording while connected" flag.
Wi-Fi dropping, the browser closing, the phone's battery dying, and the
user walking out of range are all *invisible* to the recording path.

The browser reconnects on its own (Socket.IO with infinite retries, falling
back to 2-second HTTP polling), and on reconnect it simply re-reads
`/api/status` — server state is the only truth.

### "Power failure loses only a few seconds"

Three mechanisms combine:

1. `arecord` is given a ~1-second ALSA buffer, so at most about a second of
   audio is in flight at any moment.
2. Before the first sample is written, a `.journal.json` file records the
   session's format and filenames. Its presence means "this take never
   reached a clean stop."
3. On startup, `recover_orphans()` finds every stale journal and calls
   `repair_wav()` on the referenced files.

### "Automatically close corrupted recordings after reboot"

`arecord` writes a placeholder WAV header and patches the length fields on
clean exit. After a power cut those fields still read zero, so players refuse
the file — even though every byte of audio is present. `repair_wav()`
recomputes the RIFF and data chunk sizes from the file's actual length and
rewrites the 44-byte header. The audio was never lost; only the header was
wrong.

This is verified by an automated test that simulates the exact failure.

---

## Design decisions and their trade-offs

### `arecord` rather than PyAudio or FFmpeg for capture

PyAudio delivers frames into Python, which puts the GIL on the critical path.
FFmpeg would work, but `arecord` is a thinner wrapper over ALSA with fewer
moving parts, and its WAV output is trivially repairable. FFmpeg is still
used, but only for post-processing where a stall is harmless.

### Metering by reading the file, not tapping the stream

The obvious approach — `arecord | tee file | python` — would place a Python
process in the audio path. Instead the meter issues a `pread` of the last
~50 ms of the growing file at 10 Hz. If metering stalls or throws, the
recording is unaffected; the meter simply shows stale values for a moment.

### Pause splits the file instead of suspending the process

`SIGSTOP` would leave audio sitting in a frozen process's buffers, unwritten.
Closing the segment means paused audio is already durable on disk. Resume
opens a new numbered part.

### Post-processing is offline, not live

The spec asks for a limiter, compressor, and noise gate alongside "CPU under
20%" and "zero dropped samples" on a Pi 3. Real-time DSP at 48 kHz stereo in
Python cannot meet all three. The resolution: capture stays bit-exact and
untouched, and DSP runs afterwards through FFmpeg's C filters, `nice`d to
priority 15 and deferred entirely while a take is active. The master WAV is
never modified — processed output is written as a separate `_processed` file.

### Wi-Fi modes are a chain, not simultaneous

The Pi 3 has a single radio that cannot reliably host an AP and maintain a
station link at once. `auto_connect()` therefore tries saved networks by
priority, then phone hotspots, then falls back to hosting `ZoomPi`.

### SQLite is optional metadata, not the source of truth

A recording is valid if the WAV exists. Deleting `zoompi.db` loses notes and
markers but never audio. WAL mode lets the recorder write markers while the
web layer reads.

---

## Threading model

| Thread | Rate | Purpose |
|---|---|---|
| Main / Werkzeug | on demand | HTTP + WebSocket |
| `rec-watchdog` | 0.5 Hz | Split limits, disk headroom, restart dead capture |
| `level-meter` | 10 Hz | Read file tail, compute RMS/peak |
| `broadcaster` | 10 Hz rec / 2 Hz idle | Push status to clients |
| `wifi-watchdog` | 0.02 Hz | Reconnect when the link drops |
| `postprocess` | on demand | FFmpeg queue, idle during takes |
| `hw-refresh` | 1 Hz | LED state + OLED redraw |

`async_mode="threading"` is used deliberately. The `eventlet` default is
incompatible with Python 3.12+ (it crashes with
`module 'eventlet.green.thread' has no attribute 'start_joinable_thread'`),
and monkey-patching the standard library would interfere with `subprocess`
management, which is exactly what must stay predictable here.

---

## Resource budget on a Pi 3

| Component | CPU | Memory |
|---|---|---|
| `arecord` (48 kHz/16-bit stereo) | 2–4% | ~5 MB |
| Level meter (10 Hz) | <1% | negligible |
| Flask + SocketIO idle | 1–2% | ~45 MB |
| Broadcaster | ~1% | negligible |
| **Total while recording** | **~8%** | **~90 MB** |

Comfortably inside the 20% CPU / 300 MB targets. Post-processing will spike
CPU, which is why it is deferred until recording stops.

Storage: 48 kHz/16-bit stereo is about **660 MB per hour**; 24-bit is about
990 MB per hour. A 128 GB card holds roughly 190 hours of 16-bit stereo.
