import unittest

import throttled_ctl as ctl


class FakeMsr:
    def __init__(self, values, fail_on_write=None, mismatch_on_read=None):
        self.values = dict(values)
        self.fail_on_write = fail_on_write
        self.mismatch_on_read = mismatch_on_read
        self.write_count = 0

    def read(self, cpu, register):
        value = self.values[(cpu, register)]
        if self.mismatch_on_read == (cpu, register, self.write_count):
            return value ^ 1
        return value

    def write(self, cpu, register, value):
        self.write_count += 1
        if self.fail_on_write == (cpu, register, self.write_count):
            raise ctl.ProbeError("synthetic write failure")
        self.values[(cpu, register)] = value

    def cpus(self):
        return sorted({cpu for cpu, _ in self.values})


class ControlEncodingTests(unittest.TestCase):
    def test_hwp_epp_preserves_other_fields(self):
        raw = 0x123456789ABCDEF0
        updated = ctl.update_hwp_epp(raw, 0x20)
        self.assertEqual((updated >> 24) & 0xFF, 0x20)
        self.assertEqual(updated & ~ctl.HWP_EPP_MASK, raw & ~ctl.HWP_EPP_MASK)

    def test_turbo_toggle_only_changes_bit_38(self):
        raw = 0x123456789ABCDEF0
        disabled = ctl.update_turbo(raw, False)
        enabled = ctl.update_turbo(disabled, True)
        self.assertTrue(ctl.bit(disabled, ctl.TURBO_DISABLE_BIT))
        self.assertFalse(ctl.bit(enabled, ctl.TURBO_DISABLE_BIT))
        self.assertEqual(raw & ~(1 << ctl.TURBO_DISABLE_BIT), enabled)

    def test_bd_prochot_toggle_only_changes_bit_zero(self):
        raw = 0x123456789ABCDEE0
        enabled = ctl.update_bd_prochot(raw, True)
        disabled = ctl.update_bd_prochot(enabled, False)
        self.assertTrue(enabled & 1)
        self.assertFalse(disabled & 1)
        self.assertEqual(raw & ~1, disabled)

    def test_power_update_preserves_unrelated_bits(self):
        units = {"power_w": 0.125, "time_s": 1 / 1024}
        original = (
            (1 << ctl.PKG_PL1_ENABLE_BIT)
            | (1 << ctl.PKG_PL1_CLAMP_BIT)
            | (1 << ctl.PKG_PL2_ENABLE_BIT)
            | (1 << 60)
            | 100
            | (200 << ctl.PKG_PL2_POWER_SHIFT)
        )
        updated, details = ctl.update_pkg_power_limit(original, units, pl1=25, pl2=44, tau1=28)
        self.assertEqual(updated & (1 << 60), original & (1 << 60))
        self.assertTrue(ctl.bit(updated, ctl.PKG_PL1_ENABLE_BIT))
        self.assertTrue(ctl.bit(updated, ctl.PKG_PL1_CLAMP_BIT))
        self.assertTrue(ctl.bit(updated, ctl.PKG_PL2_ENABLE_BIT))
        self.assertAlmostEqual(details["pl1_w"], 25)
        self.assertAlmostEqual(details["pl2_w"], 44)
        self.assertGreater(details["tau1_s"], 0)

    def test_power_update_refuses_locked_register(self):
        with self.assertRaises(ctl.ControlError):
            ctl.update_pkg_power_limit(
                1 << ctl.PKG_POWER_LOCK_BIT,
                {"power_w": 0.125, "time_s": 1 / 1024},
                pl1=15,
            )

    def test_time_window_selects_nearest_encoding(self):
        field, represented = ctl.encode_time_window(28.0, 1 / 1024)
        self.assertGreaterEqual(field, 0)
        self.assertLessEqual(field, 0x7F)
        candidates = [
            (2**y) * (1.0 + z / 4.0) / 1024
            for y in range(32)
            for z in range(4)
        ]
        self.assertAlmostEqual(abs(represented - 28.0), min(abs(value - 28.0) for value in candidates))


class TransactionTests(unittest.TestCase):
    def test_transaction_writes_and_verifies(self):
        msr = FakeMsr({(0, "A"): 1, (1, "A"): 1})
        results = ctl.apply_transaction(msr, [(0, "A", 2), (1, "A", 2)])
        self.assertEqual(msr.values[(0, "A")], 2)
        self.assertEqual(msr.values[(1, "A")], 2)
        self.assertTrue(all(item["changed"] for item in results))

    def test_transaction_rolls_back_after_failure(self):
        msr = FakeMsr(
            {(0, "A"): 1, (1, "A"): 1},
            fail_on_write=(1, "A", 2),
        )
        with self.assertRaises(ctl.ControlError):
            ctl.apply_transaction(msr, [(0, "A", 2), (1, "A", 2)])
        self.assertEqual(msr.values[(0, "A")], 1)
        self.assertEqual(msr.values[(1, "A")], 1)


class ParserTests(unittest.TestCase):
    def test_epp_range(self):
        parser = ctl.build_parser()
        self.assertEqual(parser.parse_args(["epp", "0"]).value, 0)
        self.assertEqual(parser.parse_args(["epp", "255"]).value, 255)
        with self.assertRaises(SystemExit):
            parser.parse_args(["epp", "256"])

    def test_power_requires_positive_numbers(self):
        parser = ctl.build_parser()
        parsed = parser.parse_args(["power", "--pl1", "25", "--tau1", "28"])
        self.assertEqual(parsed.pl1, 25)
        self.assertEqual(parsed.tau1, 28)


if __name__ == "__main__":
    unittest.main()
