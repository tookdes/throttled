import configparser
import tempfile
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import throttled_ctl as ctl


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

    def test_daemon_recovery_file_is_consumable_by_throttlectl(self):
        throttled = load_throttled()
        cpu = {
            "vendor_id": "GenuineIntel",
            "family": 6,
            "model": 142,
            "stepping": 12,
        }
        dmi = {"product": throttled.TARGET_DMI_PRODUCT}
        values = {
            "CORE": -50,
            "GPU": 0,
            "CACHE": -40,
            "UNCORE": 0,
            "ANALOGIO": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(throttled, "OC_BACKUP_DIR", tmp):
                with mock.patch.object(throttled, "probe_read_cpuinfo", return_value=cpu):
                    with mock.patch.object(throttled, "probe_read_dmi", return_value=dmi):
                        path = throttled.write_daemon_oc_recovery("fivr", values)

            identity = {
                "target": throttled.TARGET_DMI_PRODUCT,
                "dmi_product": throttled.TARGET_DMI_PRODUCT,
                "cpu": cpu,
            }
            with mock.patch.object(ctl, "_current_machine_identity", return_value=identity):
                loaded = ctl.load_oc_recovery(path, "fivr")

        self.assertEqual(
            loaded,
            {name.lower(): value for name, value in values.items()},
        )

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

    def test_daemon_undervolt_rolls_back_all_changed_planes_after_partial_failure(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({"UNDERVOLT.AC": {"CORE": "-50", "CACHE": "-40"}})
        state = {plane: 0 for plane in throttled.VOLTAGE_PLANES}
        id_to_plane = {value: key for key, value in throttled.VOLTAGE_PLANES.items()}
        cache_target = throttled.calc_undervolt_msr("CACHE", -40) & 0xFFE00000

        def get_uv(plane=None, convert=False):
            return {plane: state[plane]}

        def execute(command):
            plane = id_to_plane[(command >> 40) & 0xFF]
            field = command & 0xFFE00000
            # Simulate a command that reports success but fails to take effect.
            if plane == "CACHE" and field == cache_target:
                return command
            state[plane] = field
            return command

        with mock.patch.object(throttled, "get_undervolt", side_effect=get_uv):
            with mock.patch.object(throttled, "execute_oc_mailbox", side_effect=execute):
                with mock.patch.object(
                    throttled,
                    "write_daemon_oc_recovery",
                    return_value="/var/lib/throttled/oc-backups/fivr-test.json",
                ) as backup:
                    with self.assertRaises(throttled.OCMailboxError):
                        throttled.undervolt(config, source="AC")

        backup.assert_called_once()
        self.assertTrue(all(value == 0 for value in state.values()))

    def test_daemon_iccmax_rolls_back_all_changed_planes_after_partial_failure(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({"ICCMAX.AC": {"CORE": "100", "CACHE": "90"}})
        state = {plane: 40 for plane in throttled.CURRENT_PLANES}
        id_to_plane = {value: key for key, value in throttled.CURRENT_PLANES.items()}
        cache_target = throttled.calc_icc_max_msr("CACHE", 90) & throttled.ICCMAX_MAX_FIELD

        def get_icc(plane=None, convert=False):
            return {plane: state[plane]}

        def execute(command):
            plane = id_to_plane[(command >> 40) & 0xFF]
            field = command & throttled.ICCMAX_MAX_FIELD
            if plane == "CACHE" and field == cache_target:
                return command
            state[plane] = field
            return command

        with mock.patch.object(throttled, "get_icc_max", side_effect=get_icc):
            with mock.patch.object(throttled, "execute_oc_mailbox", side_effect=execute):
                with mock.patch.object(
                    throttled,
                    "write_daemon_oc_recovery",
                    return_value="/var/lib/throttled/oc-backups/iccmax-test.json",
                ) as backup:
                    with self.assertRaises(throttled.OCMailboxError):
                        throttled.set_icc_max(config, source="AC")

        backup.assert_called_once()
        self.assertEqual(state, {plane: 40 for plane in throttled.CURRENT_PLANES})

    def test_undervolt_verifies_readback_even_without_debug(self):
        throttled = load_throttled()
        config = configparser.ConfigParser()
        config.read_dict({"UNDERVOLT.AC": {"CORE": "-50"}})
        state = {plane: 0 for plane in throttled.VOLTAGE_PLANES}

        def get_uv(plane=None, convert=False):
            return {plane: state[plane]}

        # Command reports success but deliberately leaves CORE unchanged.
        with mock.patch.object(throttled, "get_undervolt", side_effect=get_uv):
            with mock.patch.object(throttled, "execute_oc_mailbox", return_value=0):
                with mock.patch.object(
                    throttled,
                    "write_daemon_oc_recovery",
                    return_value="/var/lib/throttled/oc-backups/fivr-test.json",
                ):
                    with self.assertRaises(throttled.OCMailboxError):
                        throttled.undervolt(config, source="AC")

        self.assertEqual(state["CORE"], 0)


if __name__ == "__main__":
    unittest.main()
