# Hardware Guide

## Bill of materials

### Required

| Item | Notes |
|---|---|
| Raspberry Pi 3B/3B+ or newer | Pi 4 recommended if you want MP3 export to finish quickly |
| microSD card, 32–256 GB | **High Endurance** (SanDisk Max Endurance, Samsung PRO Endurance). A regular card will fail under continuous writes |
| USB audio interface with line input | See the note on 24-bit below |
| Power supply, 5 V **2.5 A minimum** | Under-voltage is the single most common cause of USB audio dropouts |

### Optional

| Item | Purpose |
|---|---|
| 2 × momentary push buttons | Record / Stop |
| 1 × LED + 330 Ω resistor | Status indicator |
| SSD1306 128×64 OLED (I²C) | Standalone status display |
| PiSugar 3 or Waveshare UPS HAT | Battery + reporting |
| DS3231 RTC module | Correct filenames with no network |

---

## A note on 24-bit

The spec asks for 24-bit/48 kHz, and the recommended interface is the
Behringer UCA202. **The UCA202 and UCA222 are 16-bit/48 kHz only** — they
physically cannot do 24-bit.

This is handled rather than ignored: `audio_devices.py` probes each candidate
format against the real hardware at startup, the Settings page only offers
what the device accepts, and if a saved setting exceeds the hardware's
capability the UI tells you what will actually be used instead of silently
recording something different.

For genuine 24-bit, use one of:

| Interface | Max format | Notes |
|---|---|---|
| Behringer UCA202/UCA222 | 16-bit / 48 kHz | Cheap, reliable, class-compliant |
| Behringer U-PHORIA UM2 | 24-bit / 48 kHz | XLR + line |
| Focusrite Scarlett Solo (3rd/4th gen) | 24-bit / 192 kHz | Needs a powered hub on a Pi 3 |
| MOTU M2 | 24-bit / 192 kHz | Excellent converters, needs external power |

Anything advertised as USB Audio Class 1.0/2.0 compliant will work without
drivers. Check power draw — a Pi 3's USB ports are limited, and a hungry
interface needs a powered hub.

---

## Connecting the mixer

Use your mixer's **line-level** output, not a headphone jack if you can avoid
it:

```
Mixer MAIN OUT / REC OUT / AUX  ──►  Interface LINE IN (L/R)
                                          │
                                          └── USB ──►  Raspberry Pi
```

Set the mixer so peaks land around **−12 to −6 dBFS** on the ZoomPi meters.
Leaving that headroom is what makes the clip indicator useful rather than
decorative.

If your mixer only has XLR outputs, use an interface with XLR inputs (UM2)
rather than an adapter cable into a consumer line input — the level mismatch
will cost you either noise floor or headroom.

---

## GPIO wiring

BCM numbering. Defaults are configurable in Settings.

```
                  Raspberry Pi GPIO Header
              ┌─────────────────────────────┐
      3V3  1  │ ○ ○ │  2   5V
    GPIO2  3  │ ○ ○ │  4   5V
    GPIO3  5  │ ○ ○ │  6   GND ─────────────┐
    GPIO4  7  │ ○ ○ │  8   GPIO14           │
      GND  9  │ ○ ○ │ 10   GPIO15           │
   GPIO17 11  │ ● ○ │ 12   GPIO18           │
   GPIO27 13  │ ● ○ │ 14   GND ─────────────┤
   GPIO22 15  │ ● ○ │ 16   GPIO23           │
      3V3 17  │ ○ ○ │ 18   GPIO24           │
              └─────────────────────────────┘
                 │ │ │                      │
                 │ │ └── GPIO22 → LED anode │
                 │ │         LED cathode → 330Ω → GND
                 │ │
                 │ └──── GPIO27 → Stop button  → GND
                 └────── GPIO17 → Record button → GND
```

### Buttons

Both buttons connect the GPIO pin to **GND**. `gpiozero` enables the Pi's
internal pull-ups, so no external resistors are needed.

```
  GPIO17 ──────┬──────  [Record button]  ────── GND
  GPIO27 ──────┬──────  [Stop button]    ────── GND
```

**Record button behaviour** cycles through the transport: idle → start,
recording → pause, paused → resume.

**Stop button behaviour** depends on the `recording_lock` setting. Unlocked,
one press stops. Locked, the first press arms (the LED flashes rapidly) and a
second press within 2 seconds confirms — this prevents a knocked panel from
ending a live take.

### Status LED

```
  GPIO22 ──── [330Ω] ──── LED(+) ─── LED(−) ──── GND
```

| Pattern | Meaning |
|---|---|
| Off | Idle |
| Solid | Recording |
| Slow blink (0.5 s) | Paused |
| Fast blink (0.15 s) | Error, or stop button armed |

### OLED display (I²C)

```
  OLED VCC ──── Pin 1  (3V3)
  OLED GND ──── Pin 6  (GND)
  OLED SDA ──── Pin 3  (GPIO2 / SDA1)
  OLED SCL ──── Pin 5  (GPIO3 / SCL1)
```

Enable I²C first:

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
i2cdetect -y 1        # should show 3c or 3d
```

Display layout:

```
┌──────────────────────┐
│ REC  01:23:45        │
│ 512.3 MB             │
│ Free 98 GB           │
│ Batt 87%             │
│ 192.168.1.19         │
└──────────────────────┘
```

### Enabling hardware support

```bash
bash install.sh --hardware
```

Then turn on **GPIO buttons & LED** (and **OLED display**) in Settings and
restart:

```bash
sudo systemctl restart zoompi
```

If `gpiozero` is not installed, the toggles still appear but Settings shows
"gpiozero not installed" and the web interface works normally. Hardware is
strictly additive — nothing breaks without it.

---

## Battery operation

Any USB power bank that supports **pass-through charging** will work for
runtime, but only a proper UPS HAT reports its level to the dashboard.

| Option | Reports level? | Notes |
|---|---|---|
| Plain USB power bank | No | Dashboard shows "AC". Check it delivers a genuine 2.5 A |
| PiSugar 3 | Yes | Read over its local socket on port 8423 |
| Waveshare UPS HAT | Yes | Exposed via `/sys/class/power_supply` |
| Geekworm X728 | Yes | Also supports safe shutdown on low battery |

Rough runtime at ~450 mA average while recording:

| Capacity | Approximate runtime |
|---|---|
| 10 000 mAh | 12–14 h |
| 20 000 mAh | 24–28 h |

Watch the dashboard's under-voltage warning. If it appears, the supply cannot
sustain the Pi plus the USB interface, and audio dropouts become likely.

---

## Real-time clock

Without a network or an RTC the Pi has no idea what time it is at boot, so
filenames will be wrong. A DS3231 costs very little and fixes this:

```
  RTC VCC ──── Pin 1  (3V3)
  RTC GND ──── Pin 9  (GND)
  RTC SDA ──── Pin 3  (GPIO2)
  RTC SCL ──── Pin 5  (GPIO3)
```

```bash
echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
sudo reboot
sudo hwclock -w        # write current system time to the RTC
sudo hwclock -r        # read it back
```

---

## Enclosure notes

- Leave airflow around the Pi. Sustained recording plus the web server keeps
  the SoC warm; above roughly 80 °C it throttles, which the dashboard reports.
- Strain-relieve the USB audio cable. A cable that can be tugged is the
  easiest way to lose a take.
- Mount buttons where they cannot be pressed accidentally, and turn on
  `recording_lock` for unattended installs.
- Bring the SD card slot out to an accessible edge if you plan to pull cards
  between events.
