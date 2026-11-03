import unittest
from configparser import ConfigParser

import throttled


class PowerProfilesDaemonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = ConfigParser()
        self.config.read_dict({
            'GENERAL': {'Enabled': 'True'},
            'AC': {'PL1_Tdp_W': '44', 'PL2_Tdp_W': '44', 'Update_Rate_s': '5'},
            'BATTERY': {'PL1_Tdp_W': '29', 'PL2_Tdp_W': '44', 'Update_Rate_s': '30'},
            'PERFORMANCE': {'PL1_Tdp_W': '51', 'PL2_Tdp_W': '51', 'Update_Rate_s': '2'},
            'PERFORMANCE.BATTERY': {'PL1_Tdp_W': '44', 'PL2_Tdp_W': '44', 'Update_Rate_s': '5'},
        })

    def test_ppd_to_config_section(self):
        self.assertEqual(throttled.ppd_to_config_section('performance'), 'PERFORMANCE')
        self.assertEqual(throttled.ppd_to_config_section('balanced'), 'BALANCED')
        self.assertEqual(throttled.ppd_to_config_section('power-saver'), 'POWER_SAVER')
        self.assertIsNone(throttled.ppd_to_config_section(None))

    def test_resolve_effective_profile_precedence(self):
        # 1. Composite section match: PERFORMANCE.BATTERY
        eff = throttled.resolve_effective_profile(self.config, source='BATTERY', ppd_profile='performance')
        self.assertEqual(eff, 'PERFORMANCE.BATTERY')

        # 2. General PPD profile section match: PERFORMANCE (since PERFORMANCE.AC is not defined)
        eff = throttled.resolve_effective_profile(self.config, source='AC', ppd_profile='performance')
        self.assertEqual(eff, 'PERFORMANCE')

        # 3. PPD profile not explicitly defined (balanced) -> falls back to AC or BATTERY
        eff = throttled.resolve_effective_profile(self.config, source='AC', ppd_profile='balanced')
        self.assertEqual(eff, 'AC')
        eff = throttled.resolve_effective_profile(self.config, source='BATTERY', ppd_profile='balanced')
        self.assertEqual(eff, 'BATTERY')

    def test_configured_profiles_includes_custom_ppd_sections(self):
        profiles = throttled.get_configured_power_profiles(self.config)
        self.assertIn('AC', profiles)
        self.assertIn('BATTERY', profiles)
        self.assertIn('PERFORMANCE', profiles)
        self.assertIn('PERFORMANCE.BATTERY', profiles)


if __name__ == '__main__':
    unittest.main()
