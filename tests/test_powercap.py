import tempfile
import unittest
from pathlib import Path

import throttled_powercap as powercap


def make_zone(root, driver="intel-rapl-mmio", name="package-0"):
    zone = Path(root) / driver / f"{driver}:0"
    zone.mkdir(parents=True)
    (zone / "name").write_text(name)
    (zone / "enabled").write_text("1")
    for index, role, power, time_us in (
        (0, "long_term", 15_000_000, 28_000_000),
        (1, "short_term", 44_000_000, 2_440),
    ):
        (zone / f"constraint_{index}_name").write_text(role)
        (zone / f"constraint_{index}_power_limit_uw").write_text(str(power))
        (zone / f"constraint_{index}_time_window_us").write_text(str(time_us))
        (zone / f"constraint_{index}_min_power_uw").write_text("1000000")
        (zone / f"constraint_{index}_max_power_uw").write_text("60000000")
        (zone / f"constraint_{index}_min_time_window_us").write_text("1")
        (zone / f"constraint_{index}_max_time_window_us").write_text("60000000")
    return zone


class PowercapTests(unittest.TestCase):
    def test_discovers_mmio_package_zone_and_named_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone_path = make_zone(tmp)
            zone = powercap.find_mmio_package_zone(Path(tmp))
            self.assertIsNotNone(zone)
            self.assertEqual(zone.path, zone_path)
            state = zone.read()
            self.assertEqual(state["pl1"]["name"], "long_term")
            self.assertEqual(state["pl2"]["name"], "short_term")
            self.assertEqual(state["pl1"]["power_limit_uw"], 15_000_000)
            self.assertEqual(state["pl2"]["power_limit_uw"], 44_000_000)

    def test_constraint_index_fallback_works_without_name_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone_path = make_zone(tmp)
            (zone_path / "constraint_0_name").unlink()
            (zone_path / "constraint_1_name").unlink()
            zone = powercap.PowercapZone(zone_path)
            self.assertEqual(zone.read_constraint("pl1")["index"], 0)
            self.assertEqual(zone.read_constraint("pl2")["index"], 1)

    def test_write_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone = powercap.PowercapZone(make_zone(tmp))
            snapshot = zone.snapshot()
            updated = zone.write(pl1_w=25, pl2_w=40, tau1_s=20, tau2_s=0.005)
            self.assertEqual(updated["pl1"]["power_limit_uw"], 25_000_000)
            self.assertEqual(updated["pl2"]["power_limit_uw"], 40_000_000)
            self.assertEqual(updated["pl1"]["time_window_us"], 20_000_000)
            self.assertEqual(updated["pl2"]["time_window_us"], 5_000)

            restored = zone.restore(snapshot)
            self.assertEqual(restored["pl1"]["power_limit_uw"], 15_000_000)
            self.assertEqual(restored["pl2"]["power_limit_uw"], 44_000_000)
            self.assertEqual(restored["pl1"]["time_window_us"], 28_000_000)
            self.assertEqual(restored["pl2"]["time_window_us"], 2_440)

    def test_write_rejects_nonfinite_and_nonpositive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone = powercap.PowercapZone(make_zone(tmp))
            for kwargs in (
                {"pl1_w": 0},
                {"pl1_w": -1},
                {"pl1_w": float("nan")},
                {"tau1_s": 0},
                {"tau1_s": float("inf")},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(powercap.PowercapError):
                        zone.write(**kwargs)

    def test_write_rejects_kernel_power_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone = powercap.PowercapZone(make_zone(tmp))
            with self.assertRaises(powercap.PowercapError):
                zone.write(pl1_w=0.5)
            with self.assertRaises(powercap.PowercapError):
                zone.write(pl2_w=61)

    def test_write_rejects_kernel_time_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone = powercap.PowercapZone(make_zone(tmp))
            with self.assertRaises(powercap.PowercapError):
                zone.write(tau1_s=61)

    def test_write_skips_values_that_are_already_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            zone = powercap.PowercapZone(make_zone(tmp))
            before = zone.read()
            after = zone.write(pl1_w=15, pl2_w=44, tau1_s=28, tau2_s=0.00244)
            self.assertEqual(after["pl1"]["power_limit_uw"], before["pl1"]["power_limit_uw"])
            self.assertEqual(after["pl2"]["power_limit_uw"], before["pl2"]["power_limit_uw"])

    def test_probe_reports_missing_driver_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = powercap.probe_powercap(Path(tmp))
            self.assertIsNone(report["msr"])
            self.assertIsNone(report["mmio"])


if __name__ == "__main__":
    unittest.main()
