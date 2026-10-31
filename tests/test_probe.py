import json
import unittest

import throttled_probe as probe


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
        self.assertEqual(decoded["active"], ["Current/EDP", "Package-PL1"])
        self.assertEqual(decoded["logged"], ["VR-Thermal"])

    def test_target_detection_accepts_both_whiskey_lake_cpuid_revisions(self):
        dmi = {"product": "HP EliteBook 830 G6"}
        for stepping in (11, 12):
            with self.subTest(stepping=stepping):
                cpu = {"family": 6, "model": 142, "stepping": stepping}
                result = probe.cpu_target_status(cpu, dmi)
                self.assertTrue(result["cpuid_match"])
                self.assertTrue(result["dmi_match"])

    def test_restore_plan_only_uses_saved_values(self):
        snapshot = {
            "package_msrs": {
                "MSR_PKG_POWER_LIMIT": {"value": "0x1234"},
                "MSR_TEMPERATURE_TARGET": {"error": "not readable"},
            },
            "per_cpu_msrs": {
                "0": {"IA32_HWP_REQUEST": {"value": "0x42"}},
                "1": {"IA32_HWP_REQUEST": {"value": "0x43"}},
            },
        }
        plan = probe.build_restore_plan(snapshot, [0, 1])
        self.assertIn((0, "MSR_PKG_POWER_LIMIT", 0x1234), plan)
        self.assertIn((1, "MSR_PKG_POWER_LIMIT", 0x1234), plan)
        self.assertIn((0, "IA32_HWP_REQUEST", 0x42), plan)
        self.assertIn((1, "IA32_HWP_REQUEST", 0x43), plan)
        self.assertFalse(any(name == "MSR_TEMPERATURE_TARGET" for _, name, _ in plan))

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


if __name__ == "__main__":
    unittest.main()
