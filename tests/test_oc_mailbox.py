import configparser
import importlib.util
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
    module.UNSUPPORTED_FEATURES.clear()
    return module


class OcMailboxTests(unittest.TestCase):
    def test_capability_probe_skips_unconfigured_oc_mailbox_features(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({
            "GENERAL": {"Enabled": "True"},
            "AC": {"Update_Rate_s": "5", "HWP_EPP": "32"},
        })
        throttled.args.monitor = None

        with mock.patch.object(throttled, "get_undervolt") as get_uv:
            with mock.patch.object(throttled, "get_icc_max") as get_icc:
                with mock.patch.object(
                    throttled,
                    "readmsr",
                    side_effect=lambda name, *args, **kwargs: 1 if name == "IA32_PM_ENABLE" else 0,
                ):
                    throttled.test_msr_rw_capabilities(config)

        get_uv.assert_not_called()
        get_icc.assert_not_called()

    def test_monitor_skips_oc_mailbox_queries_when_features_are_unconfigured(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({"GENERAL": {"Enabled": "True"}, "AC": {"Update_Rate_s": "5"}})

        class StopImmediately:
            def is_set(self):
                return True

        with mock.patch.object(throttled, "get_undervolt") as get_uv:
            with mock.patch.object(throttled, "get_icc_max") as get_icc:
                with mock.patch.object(throttled, "log"):
                    throttled.MONITOR_POWER_DOMAINS.clear()
                    throttled.monitor(StopImmediately(), 1, None, config)

        get_uv.assert_not_called()
        get_icc.assert_not_called()

    def test_execute_waits_for_ready_and_checks_completion_status(self):
        throttled = load_throttled()
        command = 0x8000001000000000
        reads = iter((0, 1 << 63, 0x12345678))
        with mock.patch.object(throttled, "readmsr", side_effect=lambda *args, **kwargs: next(reads)):
            with mock.patch.object(throttled, "writemsr") as write:
                with mock.patch.object(throttled, "sleep"):
                    result = throttled.execute_oc_mailbox(command)

        self.assertEqual(result, 0x12345678)
        write.assert_called_once_with("MSR_OC_MAILBOX", command, cpu=0)

    def test_execute_rejects_nonzero_mailbox_status(self):
        throttled = load_throttled()
        result = 0x07 << throttled.OC_MAILBOX_STATUS_SHIFT
        with mock.patch.object(throttled, "readmsr", side_effect=(0, result)):
            with mock.patch.object(throttled, "writemsr"):
                with self.assertRaises(throttled.OCMailboxError):
                    throttled.execute_oc_mailbox(0x8000001000000000)

    def test_undervolt_zero_does_not_write_when_already_zero(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "UNDERVOLT.AC": {
                    "CORE": "0",
                    "GPU": "0",
                    "CACHE": "0",
                    "UNCORE": "0",
                    "ANALOGIO": "0",
                }
            }
        )
        with mock.patch.object(throttled, "get_undervolt", return_value={
            plane: 0 for plane in throttled.VOLTAGE_PLANES
        }):
            with mock.patch.object(throttled, "execute_oc_mailbox") as execute:
                throttled.undervolt(config, source="AC")
        execute.assert_not_called()

    def test_undervolt_verifies_readback_even_without_debug(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({"UNDERVOLT.AC": {"CORE": "-50"}})
        desired = throttled.calc_undervolt_msr("CORE", -50) & 0xFFE00000
        values = {
            "CORE": [0, desired ^ (1 << 21)],
            "GPU": [0, 0],
            "CACHE": [0, 0],
            "UNCORE": [0, 0],
            "ANALOGIO": [0, 0],
        }

        def get_uv(plane=None, convert=False):
            value = values[plane].pop(0)
            return {plane: value}

        with mock.patch.object(throttled, "get_undervolt", side_effect=get_uv):
            with mock.patch.object(throttled, "execute_oc_mailbox"):
                with self.assertRaises(throttled.OCMailboxError):
                    throttled.undervolt(config, source="AC")


if __name__ == "__main__":
    unittest.main()
