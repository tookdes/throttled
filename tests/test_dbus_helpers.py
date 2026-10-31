import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from dbus_fast import DBusError, ErrorType


ROOT = Path(__file__).resolve().parents[1]


def load_throttled():
    spec = importlib.util.spec_from_file_location('throttled_under_test', ROOT / 'throttled.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Variant:
    def __init__(self, value):
        self.value = value


class DBusHelperTests(unittest.TestCase):
    def test_ac_signal_callback_updates_power_source(self):
        throttled = load_throttled()
        throttled.power['source'] = 'BATTERY'
        throttled.power['method'] = 'polling'

        throttled.handle_ac_properties_changed('org.freedesktop.UPower', {'OnBattery': Variant(False)}, [])

        self.assertEqual(throttled.power, {'source': 'AC', 'method': 'dbus'})

    def test_resume_callback_reapplies_settings_only_after_wake(self):
        throttled = load_throttled()
        throttled.power['source'] = 'AC'
        config = throttled.configparser.ConfigParser()
        config.add_section('GENERAL')
        config.set('GENERAL', 'Enabled', 'True')

        with mock.patch.object(throttled, 'find_mmio_package_zone', return_value=None):
            with mock.patch.object(throttled, 'apply_runtime_profile') as apply:
                throttled.handle_sleep_prepare(True, config)
                apply.assert_not_called()

                throttled.handle_sleep_prepare(False, config)
                apply.assert_called_once()
                self.assertEqual(apply.call_args.args[:3], (config, None, 'AC'))
                self.assertTrue(apply.call_args.kwargs['apply_mailbox'])
                self.assertTrue(apply.call_args.kwargs['apply_hwp'])

    def test_dbus_resume_signal_enabled_when_undervolt_or_iccmax_configured(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()
        config.add_section('GENERAL')
        config.set('GENERAL', 'Enabled', 'True')
        config.add_section('UNDERVOLT')
        config.set('UNDERVOLT', 'CORE', '-50')

        self.assertIs(throttled.should_listen_for_resume(config), True)

    def test_dbus_resume_signal_enabled_for_tpl_or_temperature_profiles(self):
        throttled = load_throttled()
        for option, value in (
            ('PL1_Tdp_W', '25'),
            ('PL1_Enable', 'True'),
            ('Trip_Temp_C', '90'),
            ('cTDP', '0'),
        ):
            with self.subTest(option=option):
                config = throttled.configparser.ConfigParser()
                config.read_dict({
                    'GENERAL': {'Enabled': 'True'},
                    'AC': {option: value},
                })
                self.assertIs(throttled.should_listen_for_resume(config), True)

    def test_dbus_resume_signal_disabled_without_undervolt_or_iccmax(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()

        self.assertIs(throttled.should_listen_for_resume(config), False)

    def test_autoreload_starts_without_login1(self):
        throttled = load_throttled()
        config = throttled.configparser.ConfigParser()
        config.read_dict({'GENERAL': {'Enabled': 'True', 'Autoreload': 'True'}})
        bus = mock.Mock()
        bus.introspect = mock.AsyncMock(
            side_effect=[object(), DBusError(ErrorType.SERVICE_UNKNOWN, 'login1 is unavailable')]
        )
        message_bus = mock.Mock()
        message_bus.return_value.connect = mock.AsyncMock(return_value=bus)
        bus_type = mock.Mock(SYSTEM=object())

        with mock.patch.object(throttled, 'get_dbus_fast', return_value=(message_bus, bus_type)):
            with mock.patch.object(throttled, 'warning') as warning:
                context = asyncio.run(throttled.setup_dbus_signal_handlers(config))

        self.assertIn('upower', context)
        warning.assert_called_once_with('login1 is unavailable; resume-time reapplication is disabled.')
        bus.disconnect.assert_not_called()


class DBusLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_disconnect_reaches_systemd(self):
        throttled = load_throttled()
        bus = mock.Mock()
        bus.wait_for_disconnect = mock.AsyncMock(side_effect=ConnectionError('bus lost'))
        context = {'bus': bus}

        with mock.patch.object(throttled, 'setup_dbus_signal_handlers', return_value=context):
            with self.assertRaisesRegex(ConnectionError, 'bus lost'):
                await asyncio.wait_for(throttled.run_dbus_loop(object()), timeout=1)

        bus.wait_for_disconnect.assert_awaited_once_with()
        bus.disconnect.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
