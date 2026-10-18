import tempfile
import unittest
from pathlib import Path

import config
from ondeskbot.calibration import CalibrationStore
from ondeskbot.errors import DeviceError
from ondeskbot.motion import MotionController
from tests.fakes import FakeMotionDriver


class CalibrationMotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calibration = CalibrationStore(
            str(Path(self.temp.name) / "calibration.json"))
        self.driver = FakeMotionDriver()
        self.motion = MotionController(self.driver, self.calibration)

    def tearDown(self):
        self.temp.cleanup()

    def test_phone_edges_map_to_verified_v6_motor_axes(self):
        old_margin = config.COORD_MARGIN
        config.COORD_MARGIN = 0.0
        try:
            left_top = self.calibration.screen_to_motor(0, 0)
            right_bottom = self.calibration.screen_to_motor(1, 1)
        finally:
            config.COORD_MARGIN = old_margin
        self.assertAlmostEqual(left_top[0], 20 / 99.55)
        self.assertAlmostEqual(left_top[1], 0)
        self.assertAlmostEqual(right_bottom[0], 90 / 99.55)
        self.assertAlmostEqual(right_bottom[1], 144 / 177)

    def test_click_offset_does_not_apply_to_swipe(self):
        no_offset = self.calibration.screen_to_motor(0.5, 0.5)
        click = self.calibration.screen_to_motor(0.5, 0.5, click_offset=True)
        self.assertLess(click[0], no_offset[0])
        self.assertGreater(click[1], no_offset[1])
        self.motion.swipe({"direction": "up"})
        self.assertEqual(self.driver.calls[-1][0], "drag")

    def test_double_tap_uses_verified_short_hold(self):
        self.motion.double_tap({"x": 0.5, "y": 0.5})
        call = self.driver.calls[-1]
        self.assertEqual(call[0], "move_click")
        self.assertEqual(call[3:], (2, config.Z_DOUBLE_TAP_HOLD_MS))

    def test_invalid_phone_geometry_is_rejected_without_camera_arm_fallback(self):
        with self.assertRaises(DeviceError) as raised:
            self.calibration.set_geometry(phone={
                "d_left": 20, "d_right": 120,
                "d_top": 0, "d_bottom": 144,
            })
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

        self.calibration.phone["d_right"] = 120
        with self.assertRaises(DeviceError) as raised:
            self.calibration.screen_to_motor(0.5, 0.5)
        self.assertEqual(raised.exception.code, "CALIBRATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
