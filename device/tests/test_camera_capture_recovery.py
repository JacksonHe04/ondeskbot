"""拍照"命令内 3 次全失败 ⇒ 硬重初始化一次再试"的回归。

真机背景（2026-10-05）：流程起跑后**确认照片三次全失败**
（`CAMERA_CAPTURE_FAILED「连续拍照失败」`）⇒ run 挂起且没有照片 ⇒ 被闸门否决。
事后排查：内存健康、相机参数正常、**本地探针 3/3 拍成** ✓
—— 差别在于探针做了**完整 `driver.begin()`**（相机硬重初始化）。

而原实现的"命令内重试"**只重新套参数、不重启相机** ✗；驱动侧那条"连续失败 N 次重启相机"
（`camera_manager._note_capture_failure`）是**轻量重启且跨调用计数** ⇒ 一条命令内可能救不回来 ✗。

⇒ 于是加一条**有界**的升级路径：**只硬重初始化一次、只多试一次**。
"""

import tempfile
import unittest
from pathlib import Path

from ondeskbot.calibration import CalibrationStore
from ondeskbot.camera_service import PhotoService
from ondeskbot.errors import DeviceError
from ondeskbot.transport.memory import MemoryTransport
from tests.fakes import FakeCameraDriver


class _RecoveringCameraDriver(FakeCameraDriver):
    """模拟真机：`capture()` 一度拍不成，**`begin()`（硬重初始化）能把它修好**。"""

    def __init__(self):
        FakeCameraDriver.__init__(self)
        self.succeeding = True
        self.begin_calls = 0
        self.always_broken = False

    def begin(self):
        self.begin_calls += 1
        self.ready = True
        if not self.always_broken:
            self.succeeding = True     # 硬重init 修好它
        return True

    def capture(self):
        if not self.succeeding:
            return None
        return self.frames[0] if self.frames else None


class CaptureHardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calibration = CalibrationStore(
            str(Path(self.temp.name) / "cal.json"))

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, driver):
        return PhotoService(driver, self.calibration, MemoryTransport())

    def test_exhausted_attempts_escalate_to_one_hard_begin(self):
        driver = _RecoveringCameraDriver()
        driver.ready = True
        driver.succeeding = False          # 相机卡住：命令内 3 次都会失败
        photo, width, height, _settings = self._service(driver).capture_bytes()
        self.assertTrue(photo, "硬重初始化之后应当拍到")
        self.assertEqual((width, height), (1600, 1200))
        self.assertEqual(driver.begin_calls, 1, "**有界**：只硬重初始化一次")

    def test_broken_camera_still_raises_and_flags_the_hard_attempt(self):
        driver = _RecoveringCameraDriver()
        driver.ready = True
        driver.succeeding = False
        driver.always_broken = True        # 连硬重init 也修不好
        with self.assertRaises(DeviceError) as raised:
            self._service(driver).capture_bytes()
        self.assertEqual(raised.exception.code, "CAMERA_CAPTURE_FAILED")
        # 调用方要能看出"硬恢复试过了"，别以为还是老形态
        self.assertIn("hard_recover_used", raised.exception.details)

    def test_healthy_camera_never_pays_the_hard_recovery(self):
        driver = _RecoveringCameraDriver()
        driver.ready = True
        photo, _w, _h, _s = self._service(driver).capture_bytes()
        self.assertTrue(photo)
        self.assertEqual(driver.begin_calls, 0, "正常路径不该走硬恢复")


if __name__ == "__main__":
    unittest.main()
