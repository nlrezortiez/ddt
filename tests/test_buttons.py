import sys
import unittest
from unittest.mock import Mock, patch

from ddt.buttons import Buttons


class ButtonsTests(unittest.TestCase):
    def test_explicit_chip_debounce_and_no_autorepeat(self):
        levels = {5: 1, 6: 1, 13: 1}
        api = Mock()
        api.gpiochip_open.return_value = 123
        api.gpio_read.side_effect = lambda handle, pin: levels[pin]
        with patch.dict(sys.modules, {'lgpio': api}), patch('ddt.buttons.time.monotonic') as clock:
            clock.return_value = 0
            buttons = Buttons(chip=0)
            api.gpiochip_open.assert_called_once_with(0)
            self.assertEqual(buttons.poll(), 0)
            levels[5] = 0
            self.assertEqual(buttons.poll(), 0)
            clock.return_value = .03
            self.assertEqual(buttons.poll(), 0)
            clock.return_value = .06
            self.assertEqual(buttons.poll(), 1)
            clock.return_value = 1
            self.assertEqual(buttons.poll(), 0)
            levels[6] = 0
            buttons.poll()
            clock.return_value = 1.06
            self.assertEqual(buttons.poll(), -1)
            buttons.close()
            buttons.close()
            api.gpiochip_close.assert_called_once_with(123)

    def test_partial_initialization_releases_chip(self):
        api = Mock()
        api.gpiochip_open.return_value = 123
        api.gpio_claim_input.side_effect = OSError('unavailable')
        with patch.dict(sys.modules, {'lgpio': api}), self.assertRaises(OSError):
            Buttons(0)
        api.gpiochip_close.assert_called_once_with(123)
