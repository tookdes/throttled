import json
import os
import struct
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import throttled_probe as probe


class FakeMsr:
    def __init__(self, values, fail_on_write=None, mismatch_after_write=None):
        self.values = dict(values)
        self.fail_on_write = fail_on_write
        self.mismatch_after_write = mismatch_after_write
        self.write_count = 0

    def read(self, cpu, register):
        value = self.values[(cpu, register)]
        if self.mismatch_after_write == (cpu, register, self.write_count):
            self.mismatch_after_write = None
            return value ^ 1
        return value

    def write(self, cpu, register, value):
        self.write_count += 1
        if self.fail_on_write == (cpu, register, self.write_count):
            raise probe.ProbeError("synthetic restore write failure")
        self.values[(cpu, register)] = value

    def cpus(self):
        return sorted({cpu for cpu, _ in self.values})


class RestoreDaemonGuardTests(unittest.TestCase):
    def test_probe_daemon_lock_detects_manual_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daemon.lock"
            path.touch()
            with path.open("a+", encoding="ascii") as held:
                probe.fcntl.flock(
                    held.fileno(),
                    probe.fcntl.LOCK_EX | probe.fcntl.LOCK_NB,
                )
                with mock.patch.object(probe, "DAEMON_LOCK_PATH", path):
                    self.assertTrue(probe.daemon_lock_active())
                probe.fcntl.flock(held.fileno(), probe.fcntl.LOCK_UN)

            with mock.patch.object(probe, "DAEMON_LOCK_PATH", path):
                self.assertFalse(probe.daemon_lock_active())

    def test_restore_refuses_active_daemon_by_default(self):
        with mock.patch.object(probe, "daemon_is_active", return_value=True):
            with self.assertRaises(probe.ProbeError):
                probe.ensure_restore_daemon_stopped(False)

    def test_restore_daemon_race_requires_explicit_override(self):
        with mock.patch.object(probe, "daemon_is_active", return_value=True):
            probe.ensure_restore_daemon_stopped(True)


class ProbeDecodeTests(unittest.TestCase):
    def test_parse_cpuinfo(self):
        cpu = probe.parse_cpuinfo(
            """processor : 0
vendor_id : GenuineIntel
cpu family : 6
model : 142
model name : Intel(R) Core(TM) i5-8365U CPU @ 1.60GHz
stepping : 12
microcode : 0xf4
flags : fpu hwp hwp_epp hwp_act_window hwp_pkg_req
"""
        )
        self.assertEqual((cpu["family"], cpu["model"], cpu["stepping"]), (6, 142, 12))
        self.assertIn("hwp_epp", cpu["flags"])

    def test_decode_rapl_power_limits(self):
        units = {"power_w": 0.125, "time_s": 1 / 1024}
        raw = (
            120
            | (1 << 15)
            | (1 << 16)
            | (10 << 17)
            | (352 << 32)
            | (1 << 47)
            | (5 << 49)
        )
        decoded = probe.decode_pkg_power_limit(raw, units)
        self.assertAlmostEqual(decoded["pl1"]["watts"], 15.0)
        self.assertAlmostEqual(decoded["pl2"]["watts"], 44.0)
        self.assertTrue(decoded["pl1"]["enabled"])
        self.assertTrue(decoded["pl1"]["clamp"])
        self.assertFalse(decoded["locked"])

    def test_decode_hwp_request(self):
        raw = 0x80 << 24 | 0x30 << 16 | 0x40 << 8 | 0x10
        decoded = probe.decode_hwp_request(raw)
        self.assertEqual(decoded["min"], 0x10)
        self.assertEqual(decoded["max"], 0x40)
        self.assertEqual(decoded["desired"], 0x30)
        self.assertEqual(decoded["epp"], 0x80)

    def test_decode_perf_limit_reasons(self):
        raw = (1 << 10) | (1 << 8) | (1 << (16 + 6))
        decoded = probe.decode_perf_limit_reasons(raw)
        self.assertEqual(decoded["active"], ["Other/EDP", "Package-PL1"])
        self.assertEqual(decoded["logged"], ["VR-Thermal"])

    def test_target_detection_accepts_both_whiskey_lake_cpuid_revisions(self):
        dmi = {"product": "HP EliteBook 830 G6"}
        for stepping in (11, 12):
            with self.subTest(stepping=stepping):
                cpu = {"family": 6, "model": 142, "stepping": stepping}
                result = probe.cpu_target_status(cpu, dmi)
                self.assertTrue(result["cpuid_match"])
                self.assertTrue(result["dmi_match"])

    def test_restore_plan_rejects_any_missing_or_unreadable_register(self):
        snapshot = {
            "power_backends": {"kernel_mmio": None, "direct_mchbar": {"available": False}},
            "package_msrs": {
                name: {"value": "0x1"} for name in probe.SNAPSHOT_PACKAGE_REGS
            },
            "per_cpu_msrs": {
                "0": {name: {"value": "0x2"} for name in probe.SNAPSHOT_PER_CPU_REGS},
                "1": {name: {"value": "0x3"} for name in probe.SNAPSHOT_PER_CPU_REGS},
            },
        }
        snapshot["package_msrs"]["MSR_TEMPERATURE_TARGET"] = {"error": "not readable"}
        with self.assertRaises(probe.ProbeError):
            probe.build_restore_plan(snapshot, [0, 1])

    def test_snapshot_schema_rejects_unknown_version_target_and_cpu_topology(self):
        base = {
            "schema_version": probe.SNAPSHOT_SCHEMA_VERSION,
            "target": probe.TARGET_DMI_PRODUCT,
            "per_cpu_msrs": {"0": {}, "1": {}},
            "power_backends": {"kernel_mmio": None, "direct_mchbar": {"available": False}},
        }
        probe.validate_snapshot_schema(base, [0, 1])

        for broken in (
            {**base, "schema_version": probe.SNAPSHOT_SCHEMA_VERSION + 1},
            {**base, "target": "Other Laptop"},
            {**base, "per_cpu_msrs": {"0": {}}},
            {**base, "per_cpu_msrs": {"cpu0": {}}},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(probe.ProbeError):
                    probe.validate_snapshot_schema(broken, [0, 1])

    def test_snapshot_identity_rejects_other_cpu(self):
        snapshot = {
            "cpu": {
                "vendor_id": "GenuineIntel",
                "family": 6,
                "model": 142,
                "stepping": 11,
            }
        }
        with self.assertRaises(probe.ProbeError):
            probe.validate_snapshot_identity(
                snapshot,
                {"vendor_id": "GenuineIntel", "family": 6, "model": 158, "stepping": 10},
            )

    def test_snapshot_identity_rejects_other_dmi_product(self):
        snapshot = {
            "cpu": {
                "vendor_id": "GenuineIntel",
                "family": 6,
                "model": 142,
                "stepping": 12,
            },
            "dmi": {"product": "HP EliteBook 830 G6"},
        }
        cpu = {"vendor_id": "GenuineIntel", "family": 6, "model": 142, "stepping": 12}
        with self.assertRaises(probe.ProbeError):
            probe.validate_snapshot_identity(snapshot, cpu, {"product": "Other Laptop"})

    def test_msr_access_uses_shared_table_for_mailbox_and_thermal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msr_path = root / "0" / "msr"
            msr_path.parent.mkdir(parents=True)
            with msr_path.open("wb") as handle:
                handle.seek(probe.MSR["IA32_THERM_STATUS"])
                handle.write(struct.pack("<Q", 0x12345678))
                handle.seek(probe.MSR["MSR_OC_MAILBOX"])
                handle.write(struct.pack("<Q", 0xABCDEF))
            access = probe.MsrAccess(root)
            self.assertEqual(access.read(0, "MSR_OC_MAILBOX"), 0xABCDEF)
            self.assertEqual(access.read(0, "IA32_THERM_STATUS"), 0x12345678)

    def test_whiskey_lake_host_bridges_are_probe_allowlisted(self):
        self.assertEqual(
            probe.MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE[0x3E34],
            probe.MCHBAR_ADDRESS_MASK_39_15,
        )
        self.assertEqual(
            probe.MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE[0x3E35],
            probe.MCHBAR_ADDRESS_MASK_39_15,
        )

    def test_snapshot_completeness_requires_every_expected_value(self):
        snapshot = {
            "package_msrs": {name: {"value": "0x1"} for name in probe.SNAPSHOT_PACKAGE_REGS},
            "per_cpu_msrs": {
                "0": {name: {"value": "0x2"} for name in probe.SNAPSHOT_PER_CPU_REGS},
            },
            "power_backends": {"kernel_mmio": None, "direct_mchbar": {"available": False}},
        }
        self.assertEqual(probe.snapshot_completeness_errors(snapshot, [0]), [])
        del snapshot["per_cpu_msrs"]["0"]["IA32_HWP_REQUEST"]
        errors = probe.snapshot_completeness_errors(snapshot, [0])
        self.assertTrue(any("IA32_HWP_REQUEST" in error for error in errors))

    def test_full_restore_verifies_captured_powercap_backend(self):
        msr = FakeMsr({(0, "MSR_PKG_POWER_LIMIT"): 0x2222})
        plan = [(0, "MSR_PKG_POWER_LIMIT", 0x1111)]
        saved_powercap = {
            "pl1": {"power_limit_uw": 15000000, "time_window_us": 28000000},
            "pl2": {"power_limit_uw": 44000000, "time_window_us": 2440},
        }
        snapshot = {
            "package_msrs": {"MSR_PKG_POWER_LIMIT": {"value": "0x0000000000001111"}},
            "power_backends": {
                "kernel_mmio": saved_powercap,
                "direct_mchbar": {"available": False},
            },
        }
        zone = mock.Mock()
        zone.snapshot.return_value = {
            "pl1": {"power_limit_uw": 10000000, "time_window_us": 1000000},
            "pl2": {"power_limit_uw": 20000000, "time_window_us": 1000},
        }
        zone.restore.return_value = saved_powercap

        with mock.patch.object(probe, "find_mmio_package_zone", return_value=zone):
            probe.apply_full_restore(msr, plan, snapshot)

        self.assertEqual(msr.values[(0, "MSR_PKG_POWER_LIMIT")], 0x1111)
        zone.restore.assert_called_once_with(saved_powercap)

    def test_snapshot_mchbar_open_allows_current_split_from_saved_value(self):
        snapshot = {
            "power_backends": {
                "direct_mchbar": {
                    "available": True,
                    "matches_msr": True,
                    "vendor_id": 0x8086,
                    "device_id": 0x3E34,
                    "pkg_power_limit_raw": 0x1111,
                }
            }
        }
        ro_mapping = mock.MagicMock()
        ro_mapping.__enter__.return_value = ro_mapping
        ro_mapping.read64.return_value = 0x2222
        rw_mapping = mock.Mock()
        rw_mapping.read64.return_value = 0x2222

        with mock.patch.object(probe, "read_host_bridge_identity", return_value=(0x8086, 0x3E34)):
            with mock.patch.object(probe, "read_mchbar_base", return_value=0xFED10000):
                with mock.patch.object(probe, "MMIO", side_effect=[ro_mapping, rw_mapping]) as mmio:
                    mapping, current_raw, error = probe._open_snapshot_mchbar(snapshot)

        self.assertIs(mapping, rw_mapping)
        self.assertEqual(current_raw, 0x2222)
        self.assertIsNone(error)
        self.assertNotEqual(current_raw, snapshot["power_backends"]["direct_mchbar"]["pkg_power_limit_raw"])
        self.assertEqual(mmio.call_count, 2)

    def test_full_restore_repairs_msr_mchbar_divergence(self):
        msr = FakeMsr({(0, "MSR_PKG_POWER_LIMIT"): 0x3333})
        plan = [(0, "MSR_PKG_POWER_LIMIT", 0x1111)]
        snapshot = {
            "package_msrs": {"MSR_PKG_POWER_LIMIT": {"value": "0x0000000000001111"}},
            "power_backends": {
                "kernel_mmio": None,
                "direct_mchbar": {
                    "available": True,
                    "matches_msr": True,
                    "pkg_power_limit_raw": 0x1111,
                },
            },
        }
        state = {"value": 0x2222}
        mapping = mock.Mock()
        mapping.read64.side_effect = lambda offset: state["value"]
        mapping.write64.side_effect = lambda offset, value: state.__setitem__("value", value)

        with mock.patch.object(
            probe,
            "_open_snapshot_mchbar",
            return_value=(mapping, 0x2222, None),
        ):
            result = probe.apply_full_restore(msr, plan, snapshot)

        self.assertEqual(msr.values[(0, "MSR_PKG_POWER_LIMIT")], 0x1111)
        self.assertEqual(state["value"], 0x1111)
        self.assertEqual(result["direct_mchbar"]["status"], "restored")
        self.assertTrue(result["complete"])
        mapping.close.assert_called_once_with()

    def test_full_restore_keeps_msr_recovery_when_direct_mchbar_is_unavailable(self):
        msr = FakeMsr({(0, "MSR_PKG_POWER_LIMIT"): 0x3333})
        plan = [(0, "MSR_PKG_POWER_LIMIT", 0x1111)]
        snapshot = {
            "package_msrs": {"MSR_PKG_POWER_LIMIT": {"value": "0x0000000000001111"}},
            "power_backends": {
                "kernel_mmio": None,
                "direct_mchbar": {
                    "available": True,
                    "matches_msr": True,
                    "pkg_power_limit_raw": 0x1111,
                },
            },
        }

        with mock.patch.object(
            probe,
            "_open_snapshot_mchbar",
            return_value=(None, None, "devmem blocked"),
        ):
            result = probe.apply_full_restore(msr, plan, snapshot)

        self.assertEqual(msr.values[(0, "MSR_PKG_POWER_LIMIT")], 0x1111)
        self.assertEqual(result["msr"]["status"], "restored")
        self.assertEqual(result["direct_mchbar"]["status"], "unavailable")
        self.assertFalse(result["complete"])

    def test_package_restore_verifies_all_online_cpu_views_without_broadcast_writes(self):
        values = {
            (0, "MSR_PKG_POWER_LIMIT"): 0x3333,
            (1, "MSR_PKG_POWER_LIMIT"): 0x3333,
        }
        msr = FakeMsr(values)
        plan = [(0, "MSR_PKG_POWER_LIMIT", 0x1111)]

        # Package-scoped fake hardware mirrors the CPU0 write into CPU1.
        original_write = msr.write
        def package_write(cpu, register, value):
            original_write(cpu, register, value)
            if register == "MSR_PKG_POWER_LIMIT":
                msr.values[(1, register)] = value
        msr.write = package_write

        snapshot = {
            "package_msrs": {"MSR_PKG_POWER_LIMIT": {"value": "0x0000000000001111"}},
            "power_backends": {
                "kernel_mmio": None,
                "direct_mchbar": {"available": False},
            },
        }
        result = probe.apply_full_restore(msr, plan, snapshot)
        self.assertEqual(result["msr"]["status"], "restored")
        self.assertEqual(msr.write_count, 1)

    def test_package_restore_rolls_back_and_stops_backends_on_cpu_view_mismatch(self):
        msr = FakeMsr({
            (0, "MSR_PKG_POWER_LIMIT"): 0x3333,
            (1, "MSR_PKG_POWER_LIMIT"): 0x2222,
        })
        plan = [(0, "MSR_PKG_POWER_LIMIT", 0x1111)]
        saved_powercap = {
            "pl1": {"power_limit_uw": 15000000, "time_window_us": 28000000},
            "pl2": {"power_limit_uw": 44000000, "time_window_us": 2440},
        }
        snapshot = {
            "package_msrs": {"MSR_PKG_POWER_LIMIT": {"value": "0x0000000000001111"}},
            "power_backends": {
                "kernel_mmio": saved_powercap,
                "direct_mchbar": {
                    "available": True,
                    "matches_msr": True,
                    "pkg_power_limit_raw": 0x1111,
                },
            },
        }

        with mock.patch.object(probe, "find_mmio_package_zone") as find_powercap:
            with mock.patch.object(probe, "_open_snapshot_mchbar") as open_mchbar:
                result = probe.apply_full_restore(msr, plan, snapshot)

        self.assertEqual(msr.values[(0, "MSR_PKG_POWER_LIMIT")], 0x3333)
        self.assertEqual(result["msr"]["status"], "failed")
        self.assertIn("cpu1", result["msr"]["reason"])
        self.assertIn("pre-restore MSR state restored", result["msr"]["reason"])
        self.assertEqual(result["kernel_mmio"]["status"], "skipped")
        self.assertEqual(result["direct_mchbar"]["status"], "skipped")
        self.assertFalse(result["complete"])
        find_powercap.assert_not_called()
        open_mchbar.assert_not_called()

    def test_snapshot_recovery_grade_distinguishes_msr_and_tpl_control(self):
        cpu = {
            "vendor_id": "GenuineIntel",
            "family": 6,
            "model": 142,
            "stepping": 12,
            "model_name": "test",
        }
        dmi = {"product": probe.TARGET_DMI_PRODUCT}
        values = {}
        for name in probe.SNAPSHOT_PACKAGE_REGS:
            values[(0, name)] = 0
        for name in probe.SNAPSHOT_PER_CPU_REGS:
            values[(0, name)] = 0
        msr = FakeMsr(values)

        with mock.patch.object(probe, "probe_powercap", return_value={"mmio": None, "msr": None}):
            with mock.patch.object(
                probe,
                "probe_mmio",
                return_value={"available": True, "matches_msr": False},
            ):
                snapshot = probe.collect_snapshot(msr, cpu, dmi)

        self.assertTrue(snapshot["recovery"]["msr_complete"])
        self.assertFalse(snapshot["recovery"]["tpl_control_complete"])
        self.assertTrue(snapshot["complete"])

    def test_safe_restore_preserves_unmanaged_temperature_target_bits(self):
        current = (100 << 16) | (5 << 24) | (1 << 40)
        saved = (95 << 16) | (10 << 24)
        msr = FakeMsr({(0, "MSR_TEMPERATURE_TARGET"): current})
        probe.apply_restore_plan(msr, [(0, "MSR_TEMPERATURE_TARGET", saved)])
        restored = msr.values[(0, "MSR_TEMPERATURE_TARGET")]
        self.assertEqual((restored >> 24) & 0x3F, 10)
        self.assertEqual((restored >> 16) & 0xFF, 100)
        self.assertEqual(restored & (1 << 40), current & (1 << 40))

    def test_safe_restore_refuses_locked_temperature_target_change(self):
        current = (100 << 16) | (5 << 24) | (1 << 31)
        saved = (100 << 16) | (10 << 24)
        msr = FakeMsr({(0, "MSR_TEMPERATURE_TARGET"): current})
        with self.assertRaises(probe.ProbeError):
            probe.apply_restore_plan(msr, [(0, "MSR_TEMPERATURE_TARGET", saved)])
        self.assertEqual(msr.values[(0, "MSR_TEMPERATURE_TARGET")], current)

    def test_safe_restore_only_changes_managed_misc_enable_bits(self):
        current = (1 << 10) | (1 << 38)
        saved = (1 << 16)
        msr = FakeMsr({(0, "IA32_MISC_ENABLE"): current})
        probe.apply_restore_plan(msr, [(0, "IA32_MISC_ENABLE", saved)])
        restored = msr.values[(0, "IA32_MISC_ENABLE")]
        self.assertTrue(restored & (1 << 10))
        self.assertTrue(restored & (1 << 16))
        self.assertFalse(restored & (1 << 38))

    def test_restore_rolls_back_after_write_failure(self):
        msr = FakeMsr(
            {
                (0, "MSR_POWER_CTL"): 1,
                (1, "MSR_POWER_CTL"): 1,
            },
            fail_on_write=(1, "MSR_POWER_CTL", 2),
        )
        plan = [
            (0, "MSR_POWER_CTL", 0),
            (1, "MSR_POWER_CTL", 0),
        ]
        with self.assertRaises(probe.ProbeError):
            probe.apply_restore_plan(msr, plan)
        self.assertEqual(msr.values[(0, "MSR_POWER_CTL")], 1)
        self.assertEqual(msr.values[(1, "MSR_POWER_CTL")], 1)

    def test_restore_rolls_back_after_verify_mismatch(self):
        msr = FakeMsr(
            {(0, "MSR_POWER_CTL"): 1},
            mismatch_after_write=(0, "MSR_POWER_CTL", 1),
        )
        with self.assertRaises(probe.ProbeError):
            probe.apply_restore_plan(msr, [(0, "MSR_POWER_CTL", 0)])
        self.assertEqual(msr.values[(0, "MSR_POWER_CTL")], 1)


if __name__ == "__main__":
    unittest.main()
