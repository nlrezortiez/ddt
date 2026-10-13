"""Set DDT_SWEEP to exercise the freshly built binary without an SDR."""
import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('DDT_SWEEP'), 'native binary not selected')
class NativeTests(unittest.TestCase):
    def test_help(self):
        result = subprocess.run([os.environ['DDT_SWEEP'], '-h'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)

    def test_invalid_arguments_fail_without_opening_usb(self):
        for args in (['-w', '0'], ['-w', '-1'], ['-w', '4294967296'],
                     ['-w', 'garbage'], ['-l', '48'], ['-l', '3'], ['-g', '63'],
                     ['-f', 'bad'], ['-f', '6000:0'], ['-N', '0'], ['-B'], ['-I']):
            with self.subTest(args=args):
                result = subprocess.run([os.environ['DDT_SWEEP'], *args], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(b'argument error', result.stderr)
                self.assertNotIn(b'hackrf_open()', result.stderr)
