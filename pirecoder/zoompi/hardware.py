"""Optional GPIO controls: buttons, status LED, OLED display.

Every import and every device access is guarded. A Pi with no HAT, no
buttons, and no display runs the full web application unchanged — hardware
is strictly additive.

Buttons use gpiozero's built-in software debounce. The stop button honours
the `recording_lock` setting: when locked it requires a double-press within
two seconds, which prevents a knocked panel from ending a live take.
"""

from __future__ import annotations

import threading
import time

from . import db
from .config import config

try:  # pragma: no cover - hardware only
    from gpiozero import LED, Button
    GPIO_AVAILABLE = True
except Exception:
    GPIO_AVAILABLE = False

try:  # pragma: no cover - hardware only
    import board
    import busio
    from PIL import Image, ImageDraw, ImageFont
    import adafruit_ssd1306
    OLED_AVAILABLE = True
except Exception:
    OLED_AVAILABLE = False

DOUBLE_PRESS_WINDOW = 2.0
LED_BLINK_INTERVAL = 0.5


class StatusLED:
    """Off = idle, solid = recording, slow blink = paused, fast = error."""

    def __init__(self, pin: int) -> None:
        self._led = None
        self._mode = "off"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if GPIO_AVAILABLE:
            try:
                self._led = LED(pin)
            except Exception:
                self._led = None

    @property
    def available(self) -> bool:
        return self._led is not None

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        if not self._led:
            return
        if mode == "recording":
            self._stop_blink()
            self._led.on()
        elif mode == "off":
            self._stop_blink()
            self._led.off()
        else:
            interval = 0.15 if mode == "error" else LED_BLINK_INTERVAL
            self._start_blink(interval)

    def _start_blink(self, interval: float) -> None:
        self._stop_blink()
        self._stop.clear()

        def blink() -> None:
            state = False
            while not self._stop.wait(interval):
                state = not state
                try:
                    self._led.on() if state else self._led.off()
                except Exception:
                    return

        self._thread = threading.Thread(target=blink, name="status-led", daemon=True)
        self._thread.start()

    def _stop_blink(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def close(self) -> None:
        self._stop_blink()
        if self._led:
            try:
                self._led.off()
                self._led.close()
            except Exception:
                pass


class OLEDDisplay:
    """128x64 SSD1306 status readout."""

    def __init__(self) -> None:
        self._display = None
        self._font = None
        self._lock = threading.Lock()
        if not OLED_AVAILABLE:
            return
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
            self._display.fill(0)
            self._display.show()
            self._font = ImageFont.load_default()
        except Exception:
            self._display = None

    @property
    def available(self) -> bool:
        return self._display is not None

    def render(self, lines: list[str]) -> None:
        if not self._display:
            return
        with self._lock:
            try:
                image = Image.new("1", (128, 64))
                draw = ImageDraw.Draw(image)
                for i, text in enumerate(lines[:5]):
                    draw.text((0, i * 12), text[:21], font=self._font, fill=255)
                self._display.image(image)
                self._display.show()
            except Exception:
                pass

    def clear(self) -> None:
        if not self._display:
            return
        with self._lock:
            try:
                self._display.fill(0)
                self._display.show()
            except Exception:
                pass


class HardwareController:
    """Wires physical controls to the recorder and mirrors state to LED/OLED."""

    def __init__(self, recorder, system_module) -> None:
        self._recorder = recorder
        self._system = system_module
        self._record_btn = None
        self._stop_btn = None
        self._led = None
        self._oled = None
        self._last_stop_press = 0.0
        self._stop_refresh = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self.enabled = False

    def start(self) -> dict:
        if not config.get("hardware_enabled"):
            return {"enabled": False, "reason": "hardware_enabled is off"}

        self._led = StatusLED(int(config.get("gpio_status_led")))
        self._oled = OLEDDisplay() if config.get("oled_enabled") else None

        if GPIO_AVAILABLE:
            try:
                self._record_btn = Button(
                    int(config.get("gpio_record_button")), pull_up=True, bounce_time=0.08
                )
                self._record_btn.when_pressed = self._on_record
                self._stop_btn = Button(
                    int(config.get("gpio_stop_button")), pull_up=True, bounce_time=0.08
                )
                self._stop_btn.when_pressed = self._on_stop
            except Exception as exc:
                db.log_event("hardware_error", {"error": str(exc)[:200]})

        self.enabled = True
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="hw-refresh", daemon=True
        )
        self._refresh_thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop_refresh.set()
        for btn in (self._record_btn, self._stop_btn):
            if btn is not None:
                try:
                    btn.close()
                except Exception:
                    pass
        if self._led:
            self._led.close()
        if self._oled:
            self._oled.clear()
        self.enabled = False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "gpio_available": GPIO_AVAILABLE,
            "oled_available": bool(self._oled and self._oled.available),
            "led_available": bool(self._led and self._led.available),
            "buttons": {
                "record": self._record_btn is not None,
                "stop": self._stop_btn is not None,
            },
        }

    # ── Button handlers ──────────────────────────────────────────────────────

    def _on_record(self) -> None:
        try:
            if self._recorder.is_paused:
                self._recorder.resume()
            elif self._recorder.is_recording:
                self._recorder.pause()
            else:
                self._recorder.start(label="button")
            db.log_event("hardware_record_press", {})
        except Exception as exc:
            db.log_event("hardware_record_error", {"error": str(exc)[:200]})

    def _on_stop(self) -> None:
        if not self._recorder.is_recording:
            return
        now = time.time()
        if config.get("recording_lock"):
            if now - self._last_stop_press > DOUBLE_PRESS_WINDOW:
                self._last_stop_press = now
                if self._led:
                    self._led.set_mode("error")  # visual "press again to confirm"
                db.log_event("hardware_stop_armed", {})
                return
        try:
            self._recorder.stop()
            db.log_event("hardware_stop_press", {})
        except Exception as exc:
            db.log_event("hardware_stop_error", {"error": str(exc)[:200]})
        finally:
            self._last_stop_press = 0.0

    # ── Display refresh ──────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        while not self._stop_refresh.wait(1.0):
            try:
                status = self._recorder.status()
                self._update_led(status)
                if self._oled and self._oled.available:
                    self._oled.render(self._compose_oled(status))
            except Exception:
                continue

    def _update_led(self, status: dict) -> None:
        if not self._led:
            return
        if status.get("last_error"):
            self._led.set_mode("error")
        elif status.get("is_paused"):
            self._led.set_mode("paused")
        elif status.get("is_recording"):
            self._led.set_mode("recording")
        else:
            self._led.set_mode("off")

    def _compose_oled(self, status: dict) -> list[str]:
        session = status.get("session")
        if status.get("is_recording"):
            state = "PAUSED" if status.get("is_paused") else "REC"
            duration = _hms(session["duration"]) if session else "00:00:00"
            size_mb = round(session["size_bytes"] / (1024 * 1024), 1) if session else 0
            head = f"{state}  {duration}"
            second = f"{size_mb} MB"
        else:
            head = config.get("device_name")
            second = "Ready"

        storage = self._system.storage()
        batt = self._system.battery()
        power = f"{batt['percent']}%" if batt.get("present") else "AC"

        return [
            head,
            second,
            f"Free {storage['free_mb'] // 1024} GB",
            f"Batt {power}",
            self._system.primary_ip(),
        ]


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
