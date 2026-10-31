import configparser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SafeDefaultConfigTests(unittest.TestCase):
    def test_shipped_config_is_opt_in_and_contains_no_active_control_values(self):
        config = configparser.ConfigParser()
        config.read(ROOT / "etc" / "throttled.conf")

        self.assertFalse(config.getboolean("GENERAL", "Enabled"))

        for profile in ("AC", "BATTERY"):
            self.assertIn(profile, config)
            # The shipped profile is intentionally inert: only polling cadence
            # may be active before the operator explicitly opts into controls.
            self.assertEqual(set(config[profile]), {"update_rate_s"})

        self.assertNotIn("UNDERVOLT.AC", config)
        self.assertNotIn("UNDERVOLT.BATTERY", config)
        self.assertNotIn("ICCMAX.AC", config)
        self.assertNotIn("ICCMAX.BATTERY", config)


if __name__ == "__main__":
    unittest.main()
