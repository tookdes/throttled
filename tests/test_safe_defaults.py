import configparser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SafeDefaultConfigTests(unittest.TestCase):
    def test_shipped_config_is_opt_in_and_contains_no_active_control_values(self):
        config = configparser.ConfigParser()
        config.read(ROOT / "etc" / "throttled.conf")

        self.assertFalse(config.getboolean("GENERAL", "Enabled"))

        write_options = {
            "PL1_Tdp_W",
            "PL1_Duration_s",
            "PL1_Enable",
            "PL1_Clamp",
            "PL2_Tdp_W",
            "PL2_Duration_s",
            "PL2_Enable",
            "PL2_Clamp",
            "Trip_Temp_C",
            "cTDP",
            "HWP_EPP",
            "HWP_Mode",
            "Disable_Turbo",
            "BDPROCHOT_Enabled",
            "Disable_BDPROCHOT",
        }
        for profile in ("AC", "BATTERY"):
            self.assertIn(profile, config)
            self.assertTrue(write_options.isdisjoint(config[profile]))

        self.assertNotIn("UNDERVOLT.AC", config)
        self.assertNotIn("UNDERVOLT.BATTERY", config)
        self.assertNotIn("ICCMAX.AC", config)
        self.assertNotIn("ICCMAX.BATTERY", config)


if __name__ == "__main__":
    unittest.main()
