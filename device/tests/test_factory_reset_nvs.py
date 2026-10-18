"""恢复出厂必须把 NVS 里的凭据副本一起擦掉。

真机事故（2026-09-29）：`erase_all()` 只擦 littlefs 上的文件，而**NVS 是独立分区、擦不到**。
串口接入协议 v1 的 `write_key` 按协议要求 2 把 key 同时写进了 NVS，于是擦除之后
`read_key`（文件优先、NVS 兜底）**仍能读出旧 key** —— "回到未绑定状态"不成立。
真机实测：`bootstrap.json` 已 MISSING，`read_key` 却照样回旧值。

与 `erase_all()` 原有的那条规矩同源：**擦不掉的绝不能报成已擦**。NVS 也一样。
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from ondeskbot import factory_reset, nvs

KEY = "b" * 64


# NVS 的命名空间与槽位由 serial 侧配置提供；这里只绑一次，免得每处都写全参数。
def _write(value):
    return nvs.write_blob(config.SERIAL_LINK_NVS_NAMESPACE,
                          config.SERIAL_LINK_NVS_KEY, value)


def _read():
    return nvs.read_blob(config.SERIAL_LINK_NVS_NAMESPACE,
                         config.SERIAL_LINK_NVS_KEY)


def _erase():
    return nvs.erase_blob(config.SERIAL_LINK_NVS_NAMESPACE,
                          config.SERIAL_LINK_NVS_KEY)


class FakeNVS:
    """够用的 `esp32.NVS` 替身。带两个失败注入开关。

    **必须按命名空间单例**：真实 NVS 是持久存储，同一个 namespace 多次 `NVS(ns)`
    拿到的是同一份数据。第一版替身每次新建空 store，于是「写进去的 key 下一次读就没了」，
    把测试自己写成了假绿 —— 跟真机上"没擦干净却看着干净"是同一类错误。
    """

    stores = {}
    # 失败注入开关挂在**类**上：每次 NVS(ns) 都会新建实例，实例级开关到了
    # erase_blob() 里的那个新实例上就失效了（"看着设了其实没生效"）。
    fail_erase = False
    swallow_erase = False

    def __init__(self, namespace):
        self.namespace = namespace
        self.store = FakeNVS.stores.setdefault(namespace, {})

    @classmethod
    def reset(cls):
        cls.stores = {}
        cls.fail_erase = False
        cls.swallow_erase = False

    def set_blob(self, key, value):
        self.store[key] = bytes(value)

    def get_blob(self, key, buffer):
        if key not in self.store:
            # 真机 absent 时的行为：抛 OSError（nvs.read_blob 会吞掉它）
            raise OSError(4354, "ESP_ERR_NVS_NOT_FOUND")
        data = self.store[key]
        buffer[: len(data)] = data
        return len(data)

    def erase_key(self, key):
        if self.fail_erase:
            raise OSError(4354, "erase failed")
        if self.swallow_erase:
            return          # 静默没擦掉：正是"报成已擦"要防的那种
        self.store.pop(key, None)

    def commit(self):
        pass


class FactoryResetNvsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patcher in (
            patch.object(config, "DATA_DIR", str(self.root)),
            patch.object(factory_reset, "MARKER_FILE",
                         str(self.root / "factory_reset.pending")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(sys.modules.pop, "esp32", None)

    def install_fake_nvs(self):
        FakeNVS.reset()
        module = types.ModuleType("esp32")
        module.NVS = FakeNVS
        sys.modules["esp32"] = module
        return FakeNVS

    # ---------------- erase_blob 的三种结局 ----------------

    def test_erase_removes_the_key_and_verifies(self):
        self.install_fake_nvs()
        self.assertIsNone(_write(KEY))
        self.assertEqual(_read(), KEY)
        self.assertEqual(_erase(), "erased")
        self.assertIsNone(_read(), "擦完还能读到 = 没擦干净")

    def test_absent_key_is_missing_not_failure(self):
        self.install_fake_nvs()
        self.assertEqual(_erase(), "missing")

    def test_silently_uneffective_erase_is_reported_as_failure(self):
        """擦除动作没抛异常、但 key 还在 —— 必须靠复核抓出来，绝不能报成已擦。"""
        self.install_fake_nvs()
        _write(KEY)
        FakeNVS.swallow_erase = True
        self.assertEqual(_erase(), "failed:key_still_present")

    def test_erase_exception_is_reported_as_failure(self):
        self.install_fake_nvs()
        _write(KEY)
        FakeNVS.fail_erase = True
        self.assertTrue(_erase().startswith("failed:"))

    def test_desktop_without_esp32_module_is_unsupported(self):
        self.assertEqual(_erase(), "unsupported")

    # ---------------- 与 erase_all 的接线 ----------------

    def test_erase_all_clears_nvs_and_reports_it(self):
        self.install_fake_nvs()
        _write(KEY)
        (self.root / "bootstrap.json").write_text("{}")

        report = factory_reset.erase_all()

        self.assertEqual(report["nvs"], "erased")
        self.assertIsNone(_read())
        self.assertFalse((self.root / "bootstrap.json").exists())
        self.assertEqual(report["failed"], [])

    def test_erase_all_surfaces_nvs_failure_in_failed_list(self):
        self.install_fake_nvs()
        _write(KEY)
        FakeNVS.swallow_erase = True

        report = factory_reset.erase_all()

        self.assertEqual(report["nvs"], "failed:key_still_present")
        self.assertIn("nvs:" + config.SERIAL_LINK_NVS_KEY, report["failed"])

    def test_erase_all_on_desktop_stays_green(self):
        """桌面（无 esp32 模块）不能因为 NVS 不可用就把擦除记成失败。"""
        report = factory_reset.erase_all()
        self.assertEqual(report["nvs"], "unsupported")
        self.assertEqual(report["failed"], [])


if __name__ == "__main__":
    unittest.main()
