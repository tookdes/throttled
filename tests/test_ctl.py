import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_bd_prochot_refuses_locked_state_change(self):
        raw = (1 << ctl.BD_PROCHOT_LOCK_BIT) | 1
        self.assertEqual(ctl.update_bd_prochot(raw, True), raw)
        with self.assertRaises(ctl.ControlError):
            ctl.update_bd_prochot(raw, False)

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

    def test_time_window_never_shortens_requested_tau(self):
        field, represented = ctl.encode_time_window(28.0, 1 / 1024)
        self.assertGreaterEqual(field, 0)
        self.assertLessEqual(field, 0x7F)
        candidates = sorted(
            (2**y) * (1.0 + z / 4.0) / 1024
            for y in range(32)
            for z in range(4)
            if (2**y) * (1.0 + z / 4.0) / 1024 >= 28.0
        )
        self.assertGreaterEqual(represented, 28.0)
        self.assertAlmostEqual(represented, candidates[0])


    def test_speedstep_and_clock_modulation_preserve_unrelated_bits(self):
        raw = 0xA5A5A5A5A5A5A5A5
        enabled = ctl.update_speedstep(raw, True)
        disabled = ctl.update_speedstep(enabled, False)
        self.assertTrue(ctl.bit(enabled, ctl.SPEEDSTEP_ENABLE_BIT))
        self.assertFalse(ctl.bit(disabled, ctl.SPEEDSTEP_ENABLE_BIT))
        self.assertEqual(
            disabled & ~(1 << ctl.SPEEDSTEP_ENABLE_BIT),
            raw & ~(1 << ctl.SPEEDSTEP_ENABLE_BIT),
        )

        full = ctl.force_clock_modulation_full(raw | (1 << ctl.CLOCK_MODULATION_ENABLE_BIT))
        self.assertFalse(ctl.bit(full, ctl.CLOCK_MODULATION_ENABLE_BIT))
        self.assertEqual(
            full & ~(1 << ctl.CLOCK_MODULATION_ENABLE_BIT),
            raw & ~(1 << ctl.CLOCK_MODULATION_ENABLE_BIT),
        )

    def test_temperature_target_preserves_tjmax_and_other_bits(self):
        raw = (100 << 16) | (5 << 24) | (1 << 40)
        updated = ctl.update_temperature_target(raw, 90)
        self.assertEqual((updated >> 16) & 0xFF, 100)
        self.assertEqual((updated >> 24) & 0x3F, 10)
        self.assertEqual(updated & (1 << 40), raw & (1 << 40))

        with self.assertRaises(ctl.ControlError):
            ctl.update_temperature_target(raw, 20)

        with self.assertRaises(ctl.ControlError):
            ctl.update_temperature_target(raw | (1 << 31), 90)

    def test_turbo_ratio_update_preserves_unspecified_bytes(self):
        raw = 0x28292A2B2C2D2E2F
        updated = ctl.update_turbo_ratios(raw, {1: 0x31, 4: 0x30})
        self.assertEqual(updated & 0xFF, 0x31)
        self.assertEqual((updated >> 24) & 0xFF, 0x30)
        self.assertEqual((updated >> 8) & 0xFFFF, (raw >> 8) & 0xFFFF)
        self.assertEqual((updated >> 32) & 0xFFFFFFFF, (raw >> 32) & 0xFFFFFFFF)

    def test_fivr_encoding_round_trip(self):
        for mv in (0, -50, -80, -125, -999, -1000):
            ticks = ctl.fivr_mv_to_ticks(mv)
            raw = ctl.encode_fivr_write_ticks("core", ticks)
            self.assertEqual(ctl.fivr_ticks_from_response(raw), ticks)
            self.assertAlmostEqual(ctl.fivr_ticks_to_mv(ticks), ticks / ctl.FIVR_TICKS_PER_MV)

    def test_iccmax_encoder_floors_to_quarter_amp_grid(self):
        self.assertEqual(ctl.iccmax_to_field(100), 400)
        self.assertEqual(ctl.iccmax_to_field(105.4), 421)
        self.assertEqual(ctl.encode_iccmax_write_field("core", 400) & 0x3FF, 400)


class MonitorMathTests(unittest.TestCase):
    def test_counter_delta_wraps(self):
        self.assertEqual(ctl.counter_delta((1 << 64) - 5, 3), 8)
        self.assertEqual(ctl.counter_delta((1 << 32) - 2, 3, 32), 5)

    def test_effective_mhz_and_c0(self):
        self.assertAlmostEqual(ctl.effective_mhz(100, 200, 300, 300, 2000), 4000)
        self.assertAlmostEqual(ctl.c0_percent(0, 1_000_000_000, 1.0, 2000), 50.0)

    def test_package_watts_handles_energy_wrap(self):
        unit = 1 / 16384
        before = (1 << 32) - 100
        after = 100
        expected = 200 * unit
        self.assertAlmostEqual(ctl.package_watts(before, after, 1.0, unit), expected)

    def test_core_temperature_uses_digital_readout(self):
        raw = (1 << 31) | (25 << 16)
        self.assertEqual(ctl.core_temperature(raw, 100), 75)
        self.assertIsNone(ctl.core_temperature(25 << 16, 100))


class MailboxTests(unittest.TestCase):
    class MailboxMsr:
        def __init__(self, reads):
            self.reads = list(reads)
            self.writes = []

        def read(self, cpu, register):
            self.assertion = (cpu, register)
            if not self.reads:
                raise AssertionError("unexpected mailbox read")
            return self.reads.pop(0)

        def write(self, cpu, register, value):
            self.writes.append((cpu, register, value))

    def test_mailbox_waits_for_completion_and_accepts_zero_status(self):
        result = 0x12345678
        msr = self.MailboxMsr([0, 1 << ctl.OC_MAILBOX_BUSY_BIT, result])
        with mock.patch.object(ctl.time, "sleep"):
            returned = ctl.execute_oc_mailbox(msr, 0x8000001000000000)
        self.assertEqual(returned, result)
        self.assertEqual(msr.writes, [(0, "MSR_OC_MAILBOX", 0x8000001000000000)])

    def test_mailbox_rejects_nonzero_completion_status(self):
        result = (0x7 << ctl.OC_MAILBOX_STATUS_SHIFT) | 0x1234
        msr = self.MailboxMsr([0, result])
        with self.assertRaises(ctl.ControlError) as caught:
            ctl.execute_oc_mailbox(msr, 0x8000001000000000)
        self.assertIn("status 0x07", str(caught.exception))


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

    def test_transaction_rolls_back_after_verify_mismatch(self):
        class VerifyMismatchMsr(FakeMsr):
            def __init__(self):
                super().__init__({(0, "A"): 1})
                self.fail_verify = True

            def read(self, cpu, register):
                value = self.values[(cpu, register)]
                if self.fail_verify and self.write_count == 1:
                    self.fail_verify = False
                    return value ^ 1
                return value

        msr = VerifyMismatchMsr()
        with self.assertRaises(ctl.ControlError):
            ctl.apply_transaction(msr, [(0, "A", 2)])
        self.assertEqual(msr.values[(0, "A")], 1)


class LegacyControlGuardTests(unittest.TestCase):
    def _args(self):
        return type("Args", (), {"force_target": False, "allow_daemon_race": False, "state": True, "ratio": 38, "force_ratio": False})()

    def test_speedstep_rejects_hwp_enabled(self):
        msr = FakeMsr({
            (0, "IA32_PM_ENABLE"): 1,
            (0, "IA32_MISC_ENABLE"): 0,
        })
        with mock.patch.object(ctl, "validate_write_environment"):
            with self.assertRaises(ctl.ControlError):
                ctl.command_speedstep(self._args(), msr)

    def test_multiplier_rejects_hwp_enabled(self):
        msr = FakeMsr({
            (0, "IA32_PM_ENABLE"): 1,
            (0, "MSR_PLATFORM_INFO"): 0,
            (0, "MSR_TURBO_RATIO_LIMIT"): 0,
            (0, "IA32_PERF_CTL"): 0,
        })
        with mock.patch.object(ctl, "validate_write_environment"):
            with self.assertRaises(ctl.ControlError):
                ctl.command_multiplier(self._args(), msr)


class PowerCommandTests(unittest.TestCase):
    def _msr(self, pkg_raw):
        # RAPL power unit = 1/8 W, time unit = 1 s.
        return FakeMsr({
            (0, "MSR_RAPL_POWER_UNIT"): 3,
            (0, "MSR_PKG_POWER_LIMIT"): pkg_raw,
        })

    def test_power_refuses_before_msr_write_without_safe_mmio_backend(self):
        msr = self._msr((120 | (1 << ctl.PKG_PL1_ENABLE_BIT)))
        args = ctl.build_parser().parse_args(["power", "--pl1", "25"])
        with mock.patch.object(ctl, "validate_write_environment"):
            with mock.patch.object(ctl, "find_mmio_package_zone", return_value=None):
                with mock.patch.object(ctl, "open_validated_ctl_mchbar", return_value=None):
                    with self.assertRaises(ctl.ControlError):
                        ctl.command_power(args, msr)
        self.assertEqual(msr.values[(0, "MSR_PKG_POWER_LIMIT")], 120 | (1 << ctl.PKG_PL1_ENABLE_BIT))
        self.assertEqual(msr.write_count, 0)

    def test_powercap_receives_msr_quantized_value(self):
        current = 120 | (1 << ctl.PKG_PL1_ENABLE_BIT)
        msr = self._msr(current)
        args = ctl.build_parser().parse_args(["power", "--pl1", "25.03"])
        zone = mock.Mock()
        zone.snapshot.return_value = {"pl1": None, "pl2": None}

        with mock.patch.object(ctl, "validate_write_environment"):
            with mock.patch.object(ctl, "find_mmio_package_zone", return_value=zone):
                ctl.command_power(args, msr)

        self.assertAlmostEqual(zone.write.call_args.kwargs["pl1_w"], 25.0)

    def test_enable_clamp_change_uses_validated_direct_mchbar(self):
        current = 120 | (1 << ctl.PKG_PL1_ENABLE_BIT)
        msr = self._msr(current)
        args = ctl.build_parser().parse_args(["power", "--pl1-enable", "off"])
        state = {"value": current}
        mapping = mock.Mock()
        mapping.read64.side_effect = lambda offset: state["value"]
        mapping.write64.side_effect = lambda offset, value: state.__setitem__("value", value)

        with mock.patch.object(ctl, "validate_write_environment"):
            with mock.patch.object(ctl, "find_mmio_package_zone", return_value=None):
                with mock.patch.object(ctl, "open_validated_ctl_mchbar", return_value=mapping):
                    ctl.command_power(args, msr)

        self.assertFalse(ctl.bit(msr.values[(0, "MSR_PKG_POWER_LIMIT")], ctl.PKG_PL1_ENABLE_BIT))
        self.assertEqual(state["value"], msr.values[(0, "MSR_PKG_POWER_LIMIT")])
        mapping.close.assert_called_once_with()


class OcRecoveryTests(unittest.TestCase):
    def test_recovery_backup_round_trip_validates_machine_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ctl, "OC_BACKUP_DIR", Path(tmp)):
                with mock.patch.object(
                    ctl,
                    "_current_machine_identity",
                    return_value={
                        "target": ctl.TARGET_DMI_PRODUCT,
                        "dmi_product": ctl.TARGET_DMI_PRODUCT,
                        "cpu": {"vendor_id": "GenuineIntel", "family": 6, "model": 142, "stepping": 12},
                    },
                ):
                    path = ctl.write_oc_recovery("fivr", {"core": -82, "cache": -82})
                    restored = ctl.load_oc_recovery(path, "fivr")

        self.assertEqual(restored, {"core": -82, "cache": -82})


class ParserTests(unittest.TestCase):
    def test_help_formats_literal_c0_percent(self):
        help_text = ctl.build_parser().format_help()
        self.assertIn("C0%/temperature", help_text)

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

    def test_nested_fivr_and_iccmax_parsers(self):
        parser = ctl.build_parser()
        fivr = parser.parse_args(["fivr", "set", "--core", "-80", "--cache", "-80"])
        self.assertEqual(fivr.core, -80)
        self.assertEqual(fivr.cache, -80)

        icc = parser.parse_args(["iccmax", "set", "--core", "100"])
        self.assertEqual(icc.core, 100)

        ratio = parser.parse_args(["turbo-ratio", "--1c", "41", "--4c", "38"])
        self.assertEqual(ratio.ratio1, 41)
        self.assertEqual(ratio.ratio4, 38)

        temp = parser.parse_args(["temp-target", "90"])
        self.assertEqual(temp.celsius, 90)

        monitor = parser.parse_args(["monitor", "--interval", "0.5", "--count", "3"])
        self.assertEqual(monitor.interval, 0.5)
        self.assertEqual(monitor.count, 3)

        with self.assertRaises(SystemExit):
            parser.parse_args(["monitor", "--count", "-1"])


if __name__ == "__main__":
    unittest.main()
