import tempfile
import unittest
from pathlib import Path

from ondeskbot.identity import DeviceIdentity, read_hardware_id


class IdentityTests(unittest.TestCase):
    def test_hardware_id_is_stable_and_cloud_secret_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "identity.json")
            first = DeviceIdentity(path, unique_id=b"chip-a")
            self.assertFalse(first.registered)
            first.apply_registration({
                "device_id": "device-001", "device_secret": "top-secret"
            }, now_ms=123)
            second = DeviceIdentity(path, unique_id=b"chip-a")
            self.assertTrue(second.registered)
            self.assertEqual(second.device_id, "device-001")
            self.assertNotIn("device_secret", second.public_dict())

            copied = DeviceIdentity(path, unique_id=b"chip-b")
            self.assertFalse(copied.registered)

    def test_hardware_id_uses_full_unique_id(self):
        self.assertEqual(read_hardware_id(b"\x01\xab"), "esp32s3-01ab")


if __name__ == "__main__":
    unittest.main()
