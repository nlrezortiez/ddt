from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ddt.config import Config
from ddt.spectrum import decode, detect

ROW = '2026-10-13, 12:00:00, 5700000000, 5705000000, 1000000, 20, -70, -60, -20, -50, -60\n'


class SpectrumTests(unittest.TestCase):
    def test_header_optional(self):
        self.assertIsNone(decode('1,0,6000'))
        self.assertIsNone(decode('3,0,100,200,300,400,500'))
        self.assertIsNotNone(decode(ROW))

    def test_frequency_and_strongest_bin(self):
        found = detect(decode(ROW), -32.2, ())
        self.assertEqual(found.frequency_hz, 5702500000)
        self.assertEqual(found.power_db, -20)
        self.assertIsNone(detect(decode(ROW), -20, ()))

    def test_masks_apply_per_bin(self):
        self.assertIsNone(detect(decode(ROW), -32.2, ((5702000000, 5703000000),)))

    def test_non_megahertz_bins_and_rounding(self):
        row = 'date,time,100000000,105000000,1666666.67,12,-50,-10,-30'
        self.assertAlmostEqual(detect(decode(row), -32, ()).frequency_hz, 102500000.005)

    def test_bad_input(self):
        for row in ('', 'bad', '1,100,0', 'date,time,1,2,0,20,-20',
                    ROW.replace('-20', 'nan'), ROW.replace('-20', 'inf'),
                    ROW.replace('5705000000', '5709000000'),
                    ROW.replace('1000000, 20', '1000000, 0')):
            with self.subTest(row=row), self.assertRaises(ValueError):
                decode(row)

    def test_negative_infinity_is_valid_silence(self):
        self.assertIsNone(detect(decode(ROW.replace('-20', '-inf')), -32.2, ()))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_command(self):
        Config().validate()
        command = replace(Config(), serial='abc').command()
        self.assertIn('estimate', command)
        self.assertIn('abc', command)

    def test_invalid_values(self):
        for kwargs in ({'lna_gain': 33}, {'vga_gain': 64}, {'bin_width_hz': 0},
                       {'threshold_db': float('nan')}, {'threshold_db': True},
                       {'startup_timeout': -1}, {'display_enabled': 'false'},
                       {'ranges_mhz': ((6000, 0),)}, {'retry_max_delay': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(Config(), **kwargs).validate()

    def test_config_file_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'typo': 1}))
            with self.assertRaises(ValueError):
                Config.load(str(path))
