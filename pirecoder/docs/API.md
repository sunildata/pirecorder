# ZoomPi REST API

Base URL: `http://<pi-ip>:5000`

All responses are JSON. Authentication is a session cookie obtained from
`POST /api/login`. Unauthenticated API calls return `401`.

**Status codes**

| Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Invalid input |
| 401 | Not authenticated |
| 404 | Not found |
| 409 | State conflict (already recording, locked, format change during take) |
| 500 | Server error |

Errors carry `{"error": "<message>"}`.

---

## Authentication

### `POST /api/login`
```json
{ "password": "zoompi" }
```
Returns `{"ok": true}` and sets the session cookie. Failures are delayed
0.5 s and logged.

### `POST /api/logout`
### `POST /api/password`
```json
{ "new_password": "newsecret" }
```
Minimum 4 characters. Stored as salted PBKDF2-SHA256, 120 000 rounds.

---

## Recording control

### `GET /api/status`
The single endpoint a reconnecting client needs.

```json
{
  "is_recording": true,
  "is_paused": false,
  "last_error": null,
  "session": {
    "session_id": "2026-07-26_14-32-05",
    "base_name": "2026-07-26_14-32-05_sunday-service",
    "folder": "2026-07-26",
    "device_name": "USB Audio CODEC",
    "sample_rate": 48000,
    "bit_depth": 16,
    "channels": 2,
    "started_at": 1785057125.4,
    "duration": 3612.5,
    "size_bytes": 693504000,
    "notes": "Main hall, board feed",
    "segments": [
      { "index": 0, "filename": "2026-07-26_14-32-05.wav",
        "started_at": 1785057125.4, "ended_at": 1785060725.4,
        "size_bytes": 2147483648 }
    ],
    "markers": [
      { "offset_seconds": 412.3, "label": "sermon start",
        "created_at": 1785057537.7 }
    ]
  },
  "levels": {
    "rms_db": [-18.4, -19.1],
    "peak_db": [-6.2, -7.0],
    "peak_hold_db": [-3.1, -4.4],
    "clip": [false, false],
    "active": true
  },
  "server_time": 1785060737.9
}
```

### `POST /api/record/start`
```json
{ "label": "sunday-service", "notes": "board feed" }
```
Both optional. `label` is sanitised and appended to the timestamp filename.

Fails with `409` if already recording, no capture device is present, or free
space is below `min_free_mb`.

### `POST /api/record/stop`
Returns the completed session plus any queued post-processing jobs.

If `recording_lock` is enabled, the first call returns `409`:
```json
{ "error": "Recording is locked", "requires_confirmation": true }
```
Resend with `{"confirm": true}`.

### `POST /api/record/pause`
Closes the current segment — paused audio is already durable on disk.

### `POST /api/record/resume`
Opens the next numbered part.

### `POST /api/record/marker`
```json
{ "label": "chorus" }
```
Returns `{"offset_seconds": 412.3, "label": "chorus", "created_at": ...}`.
Offset excludes paused time.

### `POST /api/record/notes`
```json
{ "notes": "Mic 2 buzzing after 20 min" }
```

### `GET /api/levels`
Levels only — lighter than `/api/status` for a polling meter.

### `POST /api/levels/reset-clip`
Clears the latched clip indicators.

---

## Files

### `GET /api/recordings`
Query: `search`, `folder`, `sort` (`date|name|size|duration`), `order` (`asc|desc`).

```json
{
  "files": [
    { "filename": "2026-07-26_14-32-05.wav", "folder": "2026-07-26",
      "size_bytes": 693504000, "size_mb": 661.4, "duration": 3612.5,
      "modified": 1785060737.9, "format": "wav" }
  ],
  "stats": { "total_files": 42, "total_size_mb": 18432.5,
             "total_duration_hours": 27.8, "folders": 6,
             "newest": "2026-07-26_14-32-05.wav" }
}
```

### `GET /api/folders`
### `GET /api/recordings/<folder>/<filename>/download`
### `GET /api/recordings/<folder>/<filename>/stream`
Supports HTTP range requests so a browser can seek without downloading a
multi-gigabyte file.

### `DELETE /api/recordings/<folder>/<filename>`
### `POST /api/recordings/delete-many`
```json
{ "items": [{ "folder": "2026-07-26", "filename": "a.wav" }] }
```

### `POST /api/recordings/<folder>/<filename>/rename`
```json
{ "new_name": "sunday-service-main" }
```
Extension is preserved. Names are restricted to letters, numbers, space, dot,
dash, underscore.

### `POST /api/recordings/export`
Returns a ZIP stream (stored, not compressed — WAV does not compress usefully
and the Pi 3 would spend minutes of CPU).

### `GET /api/folders/<folder>/export`
### `POST /api/storage/cleanup`
```json
{ "force": true }
```

---

## Sessions

### `GET /api/sessions`
### `GET /api/sessions/<session_id>`
### `POST /api/sessions/<session_id>`
```json
{ "event_name": "Sunday Service", "notes": "..." }
```

---

## System

### `GET /api/system`
```json
{
  "cpu_percent": 7.2,
  "cpu_temp_c": 52.1,
  "memory": { "total_mb": 926, "used_mb": 214, "percent": 23.1 },
  "storage": { "total_mb": 122880, "used_mb": 18432, "free_mb": 104448,
               "percent_used": 15.0, "recording_hours_left": 158.3 },
  "battery": { "present": true, "percent": 87, "status": "Discharging",
               "source": "pisugar" },
  "throttled": { "available": true, "under_voltage_now": false,
                 "throttled_now": false,
                 "under_voltage_since_boot": false,
                 "throttled_since_boot": false },
  "ip": "192.168.1.19",
  "uptime": { "seconds": 14322, "human": "3h 58m" },
  "hostname": "zoompi"
}
```

`throttled.under_voltage_now` is worth surfacing prominently — an inadequate
power supply is the most common cause of USB audio dropouts on a Pi.

### `GET /api/devices`
Enumerates capture devices with **probed** capabilities, so the UI only
offers formats the hardware will actually accept.

```json
{
  "devices": [
    { "card": 1, "device": 0, "card_id": "CODEC", "name": "USB Audio CODEC",
      "alsa_id": "hw:1,0", "is_usb": true,
      "supported_rates": [48000, 44100], "supported_depths": [16],
      "max_channels": 2 }
  ],
  "active": { "...": "same shape" }
}
```

### `GET /api/system/events`
### `GET /api/jobs`
### `GET /api/health`
**Unauthenticated** so systemd and uptime monitors can probe it.

---

## Input gain

Gain is driven as a **percentage** of the ALSA control's own range. Every
capture control accepts a percentage, while dB is optional and device-specific,
so `db` is reported for display only and never used to command a change.

### `GET /api/gain`
```json
{ "supported": true, "percent": 75, "db": 18.0, "control": "Mic" }
```

`supported: false` means the interface exposes no software capture volume — its
gain is analogue only, set with a physical knob.

### `POST /api/gain`
```json
{ "percent": 60 }
```

Applies the level, then reads it **back** from the hardware and returns the
real state, since devices quantise to their own step size:

```json
{ "supported": true, "percent": 59, "db": 12.0, "control": "Mic",
  "ok": true, "requested": 60 }
```

`ok` is false when the device settled more than 5 % from the request. The
accepted value is saved to `capture_gain_percent` and re-applied on startup and
before every take, because ALSA forgets mixer levels across a reboot.

### `GET /api/gain/controls`
Diagnostics: every capture volume control `amixer` reports for the card.

---

## Settings

### `GET /api/settings`
Returns current settings plus hardware `capabilities`. `password` and
`ap_password` are redacted.

```json
{
  "settings": { "...": "see the table below" },
  "capabilities": {
    "rates": [96000, 48000, 44100],
    "depths": [24, 16],
    "max_channels": 2,
    "ffmpeg": true,
    "mp3": true,
    "flac": true
  }
}
```

`rates`, `depths` and `max_channels` come from probing the attached interface;
`mp3` and `flac` report whether the installed FFmpeg build actually carries
each encoder, which a trimmed build may not.

### `POST /api/settings`
Accepts any subset of settings keys. Unknown keys are reported in
`rejected` rather than causing a failure.

Changing `sample_rate`, `bit_depth`, `channels`, or `audio_device` during a
recording returns `409`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `device_name` | str | `ZoomPi` | Shown in the UI and on the OLED |
| `auth_enabled` | bool | `true` | |
| `sample_rate` | int | `48000` | 44100–192000; snapped to the nearest probed rate |
| `bit_depth` | int | `16` | 16 / 24 / 32; steps *down* if unsupported |
| `channels` | int | `2` | 1 = mono; clamped to `max_channels` |
| `audio_device` | str | `auto` | `auto` prefers USB |
| `capture_gain_percent` | int | `75` | Input gain; also settable live via `POST /api/gain` |
| `auto_split_mb` | int | `2048` | 0 disables |
| `auto_split_minutes` | int | `0` | 0 disables |
| `recording_lock` | bool | `false` | Confirmation required to stop |
| `dual_recording` | bool | `false` | −12 dB safety take |
| `output_format` | str | `wav` | `wav`, `wav+flac`, `wav+mp3`, `wav+flac+mp3` |
| `mp3_bitrate` | str | `192k` | |
| `flac_compression` | int | `5` | 0–12 |
| `auto_cleanup` | bool | `false` | Delete oldest when full |
| `cleanup_threshold_pct` | int | `90` | |
| `min_free_mb` | int | `500` | Refuse to start below this |
| `post_highpass_hz` | int | `0` | |
| `post_limiter` / `post_compressor` / `post_noise_gate` | bool | `false` | Offline only |
| `wifi_mode` | str | `auto` | `auto` / `client` / `ap` |
| `ap_ssid` / `ap_password` / `ap_channel` | | `ZoomPi` / `zoompi12345` / `7` | |
| `hardware_enabled` / `oled_enabled` | bool | `false` | Restart required |
| `gpio_record_button` / `gpio_stop_button` / `gpio_status_led` | int | 17 / 27 / 22 | BCM numbering |

---

## Wi-Fi

### `GET /api/wifi/status`
### `GET /api/wifi/scan`
### `GET /api/wifi/networks`
Saved networks. PSKs are never returned.

### `POST /api/wifi/connect`
```json
{ "ssid": "VenueWiFi", "password": "...", "is_hotspot": false, "priority": 10 }
```

### `POST /api/wifi/ap/start`
### `POST /api/wifi/ap/stop`
### `POST /api/wifi/auto`
Runs the full chain: saved networks → phone hotspots → own AP.

### `DELETE /api/wifi/networks/<ssid>`

---

## WebSocket events

Connect to the same origin with Socket.IO. On connect the server immediately
emits `status` and `system`, so a reconnecting phone shows the truth without
issuing a request.

| Event | Payload | When |
|---|---|---|
| `status` | status object | Idle, ~2 Hz |
| `levels` | `{levels, status}` | Recording, ~10 Hz |
| `system` | system snapshot | Every 5 s |
| `recording_started` / `recording_stopped` | status / session | Transport |
| `recording_paused` / `recording_resumed` | status | Transport |
| `marker_added` | `{marker}` | Marker inserted |
| `segment_split` | `{segment}` | Auto-split rolled a file |
| `capture_restarted` | `{error}` | Watchdog recovered a dead capture |
| `capture_failed` | `{error}` | Capture could not be restarted |
| `storage_full` | `{error}` | Recording stopped for space |

If the WebSocket is unavailable the client falls back to polling
`/api/status` every 2 seconds. Recording is unaffected either way.

---

## Example: record for an hour with markers

```bash
BASE=http://192.168.1.19:5000
curl -s -c jar -X POST $BASE/api/login \
     -H 'Content-Type: application/json' -d '{"password":"zoompi"}'

curl -s -b jar -X POST $BASE/api/record/start \
     -H 'Content-Type: application/json' \
     -d '{"label":"sunday-service","notes":"board feed"}'

curl -s -b jar -X POST $BASE/api/record/marker \
     -H 'Content-Type: application/json' -d '{"label":"sermon"}'

curl -s -b jar $BASE/api/status | python3 -m json.tool

curl -s -b jar -X POST $BASE/api/record/stop \
     -H 'Content-Type: application/json' -d '{"confirm":true}'
```
