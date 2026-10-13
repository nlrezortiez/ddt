"""Polled pull-up inputs: explicit Linux chip, no board-number heuristics."""
import logging
import time

LOG = logging.getLogger(__name__)


class Buttons:
    PINS = ((5, 1), (6, -1), (13, 0))

    def __init__(self, chip):
        import lgpio
        self.api = lgpio
        self.handle = lgpio.gpiochip_open(chip)
        self.states = {}
        try:
            for pin, _ in self.PINS:
                lgpio.gpio_claim_input(self.handle, pin, lgpio.SET_PULL_UP)
                level = lgpio.gpio_read(self.handle, pin)
                self.states[pin] = [level, level, time.monotonic()]
        except Exception:
            self.close()
            raise

    def poll(self):
        change = 0
        now = time.monotonic()
        for pin, delta in self.PINS:
            level = self.api.gpio_read(self.handle, pin)
            candidate, stable, changed_at = self.states[pin]
            if level != candidate:
                candidate, changed_at = level, now
            if level != stable and now - changed_at >= .05:
                stable = level
                if level == 0:
                    change += delta
            self.states[pin] = [candidate, stable, changed_at]
        return change

    def close(self):
        if self.handle is not None:
            try:
                self.api.gpiochip_close(self.handle)
            except Exception:
                LOG.warning('GPIO close failed', exc_info=True)
            self.handle = None
