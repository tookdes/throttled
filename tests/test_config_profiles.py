import importlib.util
import os
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_throttled():
    spec = importlib.util.spec_from_file_location('throttled_under_test', ROOT / 'throttled.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.args = SimpleNamespace(log=None, debug=False, config='/tmp/throttled.conf', monitor=None)
    module.log_history.clear()
    module.power['source'] = 'AC'
    module.power['method'] = 'dbus'
    return module


def write_config(contents):
    config_file = tempfile.NamedTemporaryFile('w', delete=False)
    with config_file:
        config_file.write(contents)
    return config_file.name


class StopAfterWait:
    def __init__(self):
        self.timeout = None
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.timeout = timeout
        self.stopped = True


class ConfigProfileTests(unittest.TestCase):
    def test_load_config_accepts_single_power_profile(self):
        throttled = load_throttled()
        config_path = write_config(
            '[GENERAL]\n'
            'Enabled: True\n'
            '\n'
            '[AC]\n'
            'Update_Rate_s: 5\n'
            'Trip_Temp_C: 95\n'
        )
        self.addCleanup(os.unlink, config_path)
        throttled.args.config = config_path

        config = throttled.load_config()

        self.assertIn('AC', config)
        self.assertNotIn('BATTERY', config)

    def test_calc_reg_values_skips_missing_power_profile(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()
        config.add_section('AC')
        config.set('AC', 'Update_Rate_s', '5')
        config.set('AC', 'Trip_Temp_C', '90')

        with mock.patch.object(throttled, 'get_critical_temp', return_value=100):
            regs = throttled.calc_reg_values(
                {'feature_programmable_temperature_target': 1, 'feature_programmable_tdp_limit': 0},
                config,
            )

        self.assertEqual(regs['AC']['MSR_TEMPERATURE_TARGET'], 10 << 24)
        self.assertNotIn('BATTERY', regs)

    def test_boolean_only_tpl_profile_preserves_unspecified_live_flags(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()
        config.add_section('AC')
        config.set('AC', 'Update_Rate_s', '5')
        config.set('AC', 'PL1_Clamp', 'True')

        current_raw = throttled._encode_pkg_power_limit(
            120,
            10,
            352,
            5,
            pl1_enabled=False,
            pl1_clamp=False,
            pl2_enabled=True,
            pl2_clamp=True,
        )
        current = {
            'RAW': current_raw,
            'LOCKED': False,
            'PL1': 120,
            'PL1_ENABLED': False,
            'PL1_CLAMP': False,
            'TW1': 10,
            'PL2': 352,
            'PL2_ENABLED': True,
            'PL2_CLAMP': True,
            'TW2': 5,
        }

        with mock.patch.object(throttled, 'warning'):
            with mock.patch.object(throttled, 'get_cur_pkg_power_limits', return_value=current):
                with mock.patch.object(throttled, 'get_power_unit', side_effect=AssertionError('not needed')):
                    regs = throttled.calc_reg_values(
                        {'feature_programmable_temperature_target': 0, 'feature_programmable_tdp_limit': 0},
                        config,
                    )

        raw = regs['AC']['MSR_PKG_POWER_LIMIT']
        self.assertFalse(bool((raw >> 15) & 1))
        self.assertTrue(bool((raw >> 16) & 1))
        self.assertTrue(bool((raw >> 47) & 1))
        self.assertTrue(bool((raw >> 48) & 1))
        self.assertEqual(raw & 0x7FFF, 120)
        self.assertEqual((raw >> 32) & 0x7FFF, 352)

    def test_numeric_tpl_profile_keeps_historical_boolean_defaults(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()
        config.add_section('AC')
        config.set('AC', 'Update_Rate_s', '5')
        config.set('AC', 'PL1_Tdp_W', '25')

        current = {
            'RAW': 0,
            'LOCKED': False,
            'PL1': 120,
            'PL1_ENABLED': False,
            'PL1_CLAMP': False,
            'TW1': 10,
            'PL2': 352,
            'PL2_ENABLED': False,
            'PL2_CLAMP': True,
            'TW2': 5,
        }

        with mock.patch.object(throttled, 'warning'):
            with mock.patch.object(throttled, 'get_cur_pkg_power_limits', return_value=current):
                with mock.patch.object(throttled, 'get_power_unit', return_value=1):
                    regs = throttled.calc_reg_values(
                        {'feature_programmable_temperature_target': 0, 'feature_programmable_tdp_limit': 0},
                        config,
                    )

        raw = regs['AC']['MSR_PKG_POWER_LIMIT']
        self.assertTrue(bool((raw >> 15) & 1))
        self.assertTrue(bool((raw >> 16) & 1))
        self.assertTrue(bool((raw >> 47) & 1))
        self.assertFalse(bool((raw >> 48) & 1))

    def test_power_thread_uses_configured_update_rate_when_current_profile_is_missing(self):
        throttled = load_throttled()
        throttled.power['source'] = 'BATTERY'
        config = throttled.configparser.ConfigParser()
        config.add_section('GENERAL')
        config.set('GENERAL', 'Autoreload', 'False')
        config.add_section('AC')
        config.set('AC', 'Update_Rate_s', '5')
        state = {'config': config, 'regs': defaultdict(dict)}
        exit_event = StopAfterWait()

        with mock.patch.object(throttled, 'read_mchbar_base', return_value=0):
            with mock.patch.object(throttled, 'MMIO'):
                throttled.power_thread(state, exit_event)

        self.assertEqual(exit_event.timeout, 5.0)


if __name__ == '__main__':
    unittest.main()
