import json
import tempfile
import unittest
from pathlib import Path

import config
from ondeskbot.migration import migrate_legacy_files


class MigrationTests(unittest.TestCase):
    def test_only_v6_hardware_configuration_is_migrated(self):
        names = [
            "MIGRATION_FILE", "WIFI_CRED_FILE", "CALIBRATION_FILE",
            "CAM_PROFILE_FILE", "LEGACY_WIFI_CRED_FILE",
            "LEGACY_SCREEN_ROI_FILE", "LEGACY_CAM_ARM_FILE",
            "LEGACY_PHONE_POS_FILE", "LEGACY_CAM_PROFILE_FILE",
        ]
        saved = {name: getattr(config, name) for name in names}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                for name in names:
                    setattr(config, name, str(root / (name.lower() + ".json")))
                Path(config.LEGACY_WIFI_CRED_FILE).write_text(json.dumps({
                    "ssid": "wifi", "password": "pw"}))
                Path(config.LEGACY_SCREEN_ROI_FILE).write_text(json.dumps({
                    "coord_version": 6, "x1": 1, "y1": 2,
                    "x2": 100, "y2": 200, "iw": 1600, "ih": 1200,
                    "instruction": "must-not-migrate",
                }))
                Path(config.LEGACY_CAM_ARM_FILE).write_text(json.dumps({
                    "coord_version": 6, "x_min": 0.1, "x_max": 0.9,
                    "y_min": 0.2, "y_max": 0.8,
                }))
                Path(config.LEGACY_PHONE_POS_FILE).write_text(json.dumps({
                    "coord_version": 6, "d_left": 20, "d_right": 90,
                    "d_top": 0, "d_bottom": 144,
                }))
                report = migrate_legacy_files()
                self.assertTrue(report["wifi"])
                calibration = json.loads(Path(config.CALIBRATION_FILE).read_text())
                self.assertEqual(calibration["coordinate_system_version"], 6)
                self.assertNotIn("instruction", json.dumps(calibration))
                self.assertIn("llm_config", report["excluded"])
            finally:
                for name, value in saved.items():
                    setattr(config, name, value)


if __name__ == "__main__":
    unittest.main()
