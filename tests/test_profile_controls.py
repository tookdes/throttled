import configparser
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_throttled():
    spec = importlib.util.spec_from_file_location("throttled_under_test", ROOT / "throttled.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.args = SimpleNamespace(log=None, debug=False, config="/tmp/throttled.conf", monitor=None)
    module.log_history.clear()
    module.power["source"] = "AC"
    module.power["method"] = "dbus"
    return module


def make_config(sections):
    config = configparser.ConfigParser()
    for section, options in sections.items():
        config.add_section(section)
        for key, value in options.items():
            config.set(section, key, str(value))
    return config


class ProfileControlTests(unittest.TestCase):
    def test_hwp_epp_prefers_direct_profile_value(self):
        throttled = load_throttled()
        config = make_config(
            {
                "AC": {"HWP_EPP": 32, "HWP_Mode": False},
                "BATTERY": {"HWP_EPP": 180},
            }
        )

        self.assertEqual(throttled._hwp_epp(config, "AC"), 32)
        self.assertEqual(throttled._hwp_epp(config, "BATTERY"), 180)

    def test_hwp_epp_keeps_legacy_hwp_mode_semantics(self):
        throttled = load_throttled()
        config = make_config({"AC": {"HWP_Mode": True}})

        self.assertEqual(throttled._hwp_epp(config, "AC"), throttled.HWP_PERFORMANCE_VALUE)
        self.assertEqual(throttled._hwp_epp(config, "BATTERY"), throttled.HWP_DEFAULT_VALUE)

    def test_set_hwp_epp_preserves_each_cpu_request(self):
        throttled = load_throttled()
        current = {
            0: 0x0000000011223344,
            2: 0x0000000055667788,
        }

        def readmsr(name, *args, cpu=None, **kwargs):
            self.assertEqual(name, "IA32_HWP_REQUEST")
            return current[cpu]

        with mock.patch.object(throttled, "_ensure_msr_module", return_value=["/dev/cpu/0/msr", "/dev/cpu/2/msr"]):
            with mock.patch.object(throttled, "readmsr", side_effect=readmsr):
                with mock.patch.object(throttled, "writemsr") as write:
                    throttled.set_hwp_epp(0x20)

        self.assertEqual(
            write.call_args_list,
            [
                mock.call("IA32_HWP_REQUEST", (current[0] & 0xFFFFFFFF00FFFFFF) | (0x20 << 24), cpu=0),
                mock.call("IA32_HWP_REQUEST", (current[2] & 0xFFFFFFFF00FFFFFF) | (0x20 << 24), cpu=2),
            ],
        )

    def test_set_hwp_epp_updates_package_request_when_package_control_is_set(self):
        throttled = load_throttled()
        current = {
            0: (1 << 42) | 0x00112233,
            1: (1 << 42) | 0x00445566,
        }
        package = 0xABCDEF0012345678

        def readmsr(name, *args, cpu=None, **kwargs):
            if name == "IA32_HWP_REQUEST_PKG":
                return package
            return current[cpu]

        with mock.patch.object(throttled, "_ensure_msr_module", return_value=["/dev/cpu/0/msr", "/dev/cpu/1/msr"]):
            with mock.patch.object(throttled, "readmsr", side_effect=readmsr):
                with mock.patch.object(throttled, "writemsr") as write:
                    throttled.set_hwp_epp(0x80)

        self.assertEqual(write.call_args_list[0], mock.call(
            "IA32_HWP_REQUEST_PKG",
            (package & 0xFFFFFFFF00FFFFFF) | (0x80 << 24),
            cpu=0,
        ))
        self.assertEqual(len(write.call_args_list), 3)

    def test_turbo_and_bd_prochot_are_reversible(self):
        throttled = load_throttled()
        cpu_values = {0: 0, 1: 0}

        def read_turbo(name, *args, cpu=None, **kwargs):
            return cpu_values[cpu]

        with mock.patch.object(throttled, "_ensure_msr_module", return_value=["/dev/cpu/0/msr", "/dev/cpu/1/msr"]):
            with mock.patch.object(throttled, "readmsr", side_effect=read_turbo):
                with mock.patch.object(throttled, "writemsr") as write:
                    throttled.set_turbo(False)

        self.assertTrue(all(call.args[1] & (1 << throttled.TURBO_DISABLE_BIT) for call in write.call_args_list))

        with mock.patch.object(throttled, "_ensure_msr_module", return_value=["/dev/cpu/0/msr", "/dev/cpu/1/msr"]):
            with mock.patch.object(throttled, "readmsr", return_value=0):
                with mock.patch.object(throttled, "writemsr") as write:
                    throttled.set_bdprochot(True)
        self.assertEqual(len(write.call_args_list), 2)
        self.assertTrue(all(call.args[1] & 1 for call in write.call_args_list))

        with mock.patch.object(throttled, "_ensure_msr_module", return_value=["/dev/cpu/0/msr", "/dev/cpu/1/msr"]):
            with mock.patch.object(throttled, "readmsr", return_value=1):
                with mock.patch.object(throttled, "writemsr") as write:
                    throttled.set_bdprochot(False)
        # Already disabled? No writes should be needed when bit 0 is clear only;
        # here the input is 1 so each CPU must be cleared.
        self.assertEqual(len(write.call_args_list), 2)
        self.assertTrue(all(not (call.args[1] & 1) for call in write.call_args_list))

    def test_load_config_validates_hwp_epp_and_new_booleans(self):
        throttled = load_throttled()
        cases = (
            "HWP_EPP: -1",
            "HWP_EPP: 256",
            "HWP_EPP: fast",
            "Disable_Turbo: perhaps",
            "PL1_Enable: perhaps",
            "PL1_Clamp: perhaps",
            "PL2_Enable: perhaps",
            "PL2_Clamp: perhaps",
        )

        for invalid in cases:
            with self.subTest(invalid=invalid):
                tmp = tempfile.NamedTemporaryFile("w", delete=False)
                tmp.write(
                    "[GENERAL]\nEnabled: True\nAutoreload: False\n"
                    "[AC]\nUpdate_Rate_s: 5\n"
                    + invalid
                    + "\n"
                )
                tmp.close()
                self.addCleanup(os.unlink, tmp.name)
                throttled.args.config = tmp.name
                with self.assertRaises(SystemExit):
                    throttled.load_config()


if __name__ == "__main__":
    unittest.main()
