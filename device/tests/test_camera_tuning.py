import tempfile
import unittest
from pathlib import Path

from ondeskbot.calibration import CalibrationStore
from ondeskbot.camera_sessions import CameraTuningSession, RoiCalibrationSession
from ondeskbot.camera_service import PhotoService
from ondeskbot.errors import DeviceError
from ondeskbot.transport.memory import MemoryTransport
from tests.fakes import FakeCameraDriver


class CameraTuningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calibration = CalibrationStore(
            str(Path(self.temp.name) / "cal.json"))
        self.calibration.set_roi({
            "x1": 100, "y1": 100, "x2": 1500, "y2": 1100,
            "image_width": 1600, "image_height": 1200,
        })
        self.driver = FakeCameraDriver()
        self.driver.begin()
        self.transport = MemoryTransport()
        self.photo = PhotoService(
            self.driver, self.calibration, self.transport)
        self.now = 1000
        self.tuning = CameraTuningSession(
            self.photo, now_provider=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_save_settings_without_maintenance_session_persists(self):
        """回归：**无维护会话**也必须能写正式参数并落盘。

        真机成因：`camera.settings.save` 的 `access` 已改成 `setup`（注册表只放行
        setup/maintenance，agent 档仍调不动），但 `PhotoService.save_settings` 里
        **还留着旧流程的** `self.require_maintenance()` ⇒ 真机实测（auth_scope=setup）回
        `{"code": "MAINTENANCE_SESSION_REQUIRED", "message": "没有有效维护会话"}` ✗，
        参数只临时写进传感器、**没有落盘** —— 新调参流程整条堵死。
        """
        called = []
        self.photo.require_maintenance = lambda: called.append(1)
        result = self.photo.save_settings({"settings": {"quality": 13}})
        self.assertTrue(result["saved"])
        self.assertEqual(called, [])                        # 不再要求维护会话
        self.assertEqual(int(self.driver.saved["quality"]), 13)   # 真的落盘了
        self.assertEqual(int(self.driver.settings["quality"]), 13)

    def test_cloud_orchestrated_sample_upload_and_cancel_restore(self):
        original = self.driver.get_camera_settings()
        started = self.tuning.begin({"session_id": "tune-1"})
        self.assertEqual(started["roi"]["x1"], 100)
        sample = self.tuning.sample({
            "session_id": "tune-1", "settings": {"quality": 20}
        })
        self.assertEqual(sample["artifact"]["uri"], "memory://mem-000001")
        self.assertEqual(self.driver.settings["quality"], 20)
        result = self.tuning.cancel({"session_id": "tune-1"})
        self.assertTrue(result["restored"])
        self.assertEqual(self.driver.settings, original)

    def test_commit_is_the_only_operation_that_saves(self):
        self.tuning.begin({"session_id": "tune-2"})
        self.tuning.sample({
            "session_id": "tune-2", "settings": {"quality": 17}
        })
        self.assertIsNone(self.driver.saved)
        result = self.tuning.commit({
            "session_id": "tune-2", "settings": {"quality": 17}
        })
        self.assertTrue(result["saved"])
        self.assertEqual(self.driver.saved["quality"], 17)

    def test_tuning_rejects_missing_roi_and_clears_session(self):
        self.calibration.clear_roi()
        with self.assertRaises(DeviceError) as raised:
            self.tuning.begin({"session_id": "tune-no-roi"})
        self.assertEqual(raised.exception.code, "CALIBRATION_REQUIRED")
        self.assertFalse(self.tuning.active)

    # ---- `cancel` 不带 session_id 时的报错要能自解释 ----
    def test_cancel_without_session_id_says_what_is_missing(self):
        """没带 `session_id` 与"带了但不对"必须**分开报**：前者是调用姿势问题。

        现场第一次就踩了这条：不带 id 时旧文案是"不存在或不匹配"，
        让人以为是"会话没了"，其实是"你没带参数"。
        """
        self.tuning.begin({"session_id": "tune-msg"})
        with self.assertRaises(DeviceError) as raised:
            self.tuning.cancel({})
        self.assertIn("必须带 session_id", raised.exception.message)
        self.assertIn("tune-msg", raised.exception.message)   # 告诉调用方当前是哪一个
        # 带了但不对 ⇒ 仍是"不匹配"那条
        with self.assertRaises(DeviceError) as wrong:
            self.tuning.cancel({"session_id": "tune-other"})
        self.assertIn("不匹配", str(wrong.exception.message))

    def test_cancel_without_session_id_when_none_active(self):
        with self.assertRaises(DeviceError) as raised:
            self.tuning.cancel({})
        self.assertIn("没有进行中的调参会话", raised.exception.message)

    def test_roi_cancel_without_session_id_says_what_is_missing(self):
        roi = RoiCalibrationSession(self.photo, self.calibration,
                                    now_provider=lambda: self.now)
        roi.begin({"session_id": "roi-msg"})
        with self.assertRaises(DeviceError) as raised:
            roi.cancel({})
        self.assertIn("必须带 session_id", raised.exception.message)
        self.assertIn("roi-msg", raised.exception.message)
        self.assertTrue(roi.cancel({"session_id": "roi-msg"})["cancelled"])


if __name__ == "__main__":
    unittest.main()
