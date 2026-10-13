"""OLED/GPIO orchestration with lazy optional hardware imports."""

import logging
import queue
import time
from .buttons import Buttons
from .diagnostics import Duration

LOG = logging.getLogger(__name__)


class Hardware:
    def __init__(self, config):
        self.config = config
        self.device = None
        self.buttons = None
        self.changes = queue.Queue(maxsize=32)
        self.next_display = 0.0
        self.next_gpio = 0.0
        self.last_frame = None
        self.display_errors = 0
        self.gpio_errors = 0
        self.display_last_error = None
        self.gpio_last_error = None
        self.display_error_at = None
        self.gpio_error_at = None
        self.display_success_at = None
        self.gpio_success_at = None
        self.display_duration = Duration()
        self.gpio_duration = Duration()

    def _change(self, value):
        try:
            self.changes.put_nowait(value)
        except queue.Full:
            pass

    def threshold_change(self):
        result = 0
        while True:
            try:
                result += self.changes.get_nowait()
            except queue.Empty:
                return result

    def tick(self, state, threshold, detection):
        now = time.monotonic()
        if self.config.gpio_enabled and not self.buttons and now >= self.next_gpio:
            self.next_gpio = now + 5
            started = time.monotonic()
            try:
                self.buttons = Buttons(self.config.gpio_chip)
                LOG.info("GPIO ready chip=%s", self.config.gpio_chip)
            except Exception as exc:
                self._gpio_error(exc)
                LOG.exception("GPIO unavailable; retry in 5 seconds")
                self._close_gpio()
            finally:
                self.gpio_duration.observe(time.monotonic() - started)
        if self.buttons is not None:
            started = time.monotonic()
            try:
                self._change(self.buttons.poll())
                self.gpio_success_at = time.monotonic()
            except Exception as exc:
                self._gpio_error(exc)
                LOG.exception("GPIO read failed; retry in 5 seconds")
                self._close_gpio()
                self.next_gpio = now + 5
            finally:
                self.gpio_duration.observe(time.monotonic() - started)
        if not self.config.display_enabled or now < self.next_display:
            return
        self.next_display = now + 0.2
        started = time.monotonic()
        attempted = False
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306
            from luma.core.render import canvas
            if self.device is None:
                attempted = True
                serial = i2c(port=self.config.i2c_port, address=self.config.i2c_address)
                self.device = ssd1306(serial, width=128, height=64)
                self.last_frame = None
                LOG.info("OLED ready")
            frame = (state, threshold, detection)
            if frame != self.last_frame:
                attempted = True
                with canvas(self.device) as draw:
                    draw.text((0, 0), f"THRS {threshold:.1f} dB", fill="white")
                    draw.text((0, 16), state.upper(), fill="white")
                    if detection is not None and state == "receiving":
                        draw.text((0, 32), f"F {detection.frequency_hz / 1e6:.3f} MHz", fill="white")
                        draw.text((0, 48), f"P {detection.power_db:.1f} dB", fill="white")
                self.last_frame = frame
                self.display_success_at = time.monotonic()
        except Exception as exc:
            attempted = True
            self.display_errors += 1
            self.display_last_error = f'{type(exc).__name__}: {exc}'[:512]
            self.display_error_at = time.monotonic()
            LOG.exception("OLED unavailable; retry in 5 seconds")
            if self.device is not None:
                try:
                    self.device.cleanup()
                except Exception:
                    LOG.warning("OLED cleanup after failure failed", exc_info=True)
            self.device = None
            self.next_display = now + 5
        finally:
            if attempted:
                self.display_duration.observe(time.monotonic() - started)

    def _gpio_error(self, exc):
        self.gpio_errors += 1
        self.gpio_last_error = f'{type(exc).__name__}: {exc}'[:512]
        self.gpio_error_at = time.monotonic()

    def diagnostics(self):
        return dict(
            display=dict(enabled=self.config.display_enabled, ready=self.device is not None,
                         errors=self.display_errors, last_error=self.display_last_error,
                         last_error_monotonic=self.display_error_at,
                         last_success_monotonic=self.display_success_at,
                         operation_duration=self.display_duration.snapshot()),
            gpio=dict(enabled=self.config.gpio_enabled, ready=self.buttons is not None,
                      errors=self.gpio_errors, last_error=self.gpio_last_error,
                      last_error_monotonic=self.gpio_error_at,
                      last_success_monotonic=self.gpio_success_at,
                      operation_duration=self.gpio_duration.snapshot()))

    def _close_gpio(self):
        if self.buttons is not None:
            self.buttons.close()
            self.buttons = None

    def close(self):
        self._close_gpio()
        if self.device is not None:
            try:
                self.device.cleanup()
            except Exception:
                LOG.exception("OLED cleanup failed")
            finally:
                self.device = None
