"""相机拍照与参数读写。云端编排的标定 / 调参会话见 `camera_sessions`。"""

import config

from .errors import (
    CAMERA_CAPTURE_FAILED,
    CAMERA_NOT_READY,
    DeviceError,
)


class PhotoService:
    def __init__(self, driver, calibration, artifact_sink):
        self.driver = driver
        self.calibration = calibration
        self.artifact_sink = artifact_sink
        self.startup_settings = None
        self.temporary_dirty = False
        self.require_maintenance = lambda: None

    def begin(self):
        if not self.driver.begin():
            raise DeviceError(CAMERA_NOT_READY, "OV5640 初始化失败")
        self.startup_settings = self.driver.get_camera_settings()
        return self.status({})

    def status(self, _args=None):
        width, height = self.driver.get_frame_size()
        return {
            "ready": bool(self.driver.is_ready()),
            "frame": {"width": int(width), "height": int(height)},
            "settings": self.driver.get_camera_settings(),
            "setting_specs": self.driver.camera_setting_specs(),
            "saved_profile": self.driver.camera_profile_exists(),
        }

    def capture_bytes(self, max_attempts=3):
        if not self.driver.is_ready():
            raise DeviceError(CAMERA_NOT_READY, "摄像头未就绪")
        settings = self.driver.get_camera_settings()
        max_attempts = min(3, max(1, int(max_attempts)))
        for attempt in range(max_attempts):
            photo = self.driver.capture()
            if photo:
                width, height = self.driver.get_frame_size()
                return photo, int(width), int(height), settings
            if attempt + 1 < max_attempts:
                try:
                    self.driver.apply_camera_settings(settings)
                except Exception:
                    pass
        # 命令内 max_attempts 次都失败 ⇒ **再做一次"硬重初始化 + 一次尝试"**（**有界：只一次**）。
        #
        # 为什么需要（真机 2026-10-05）：驱动侧那条"连续失败 N 次就重启相机"
        # （`camera_manager._note_capture_failure`）是**轻量重启**、且**跨调用**计数 ——
        # 本次命令内它可能刚好被跳过、或不足以把相机救回来，于是**一条命令内 3 次全灭**，
        # 表现为流程卡在"没有确认照片"上。
        # 真机实测：**完整的 `driver.begin()` 能恢复**。
        # **仍然有界**：只重初始化一次、只多试一次；再不行就照旧抛 `CAMERA_CAPTURE_FAILED`，
        # 并且把"硬恢复试过"记进 `details`，别让调用方以为还是老形态。
        hard_used = False
        try:
            if self.driver.begin():
                hard_used = True
                photo = self.driver.capture()
                if photo:
                    width, height = self.driver.get_frame_size()
                    return photo, int(width), int(height), settings
        except Exception:
            pass
        raise DeviceError(CAMERA_CAPTURE_FAILED, "连续拍照失败", {
            "attempts": max_attempts,
            "hard_recover_used": hard_used,
        }, retryable=True)

    def capture(self, args=None, extra_metadata=None):
        args = args or {}
        photo, width, height, settings = self.capture_bytes(
            args.get("max_attempts", 3))
        metadata = self.calibration.photo_metadata(width, height)
        metadata["camera_settings"] = settings
        metadata["purpose"] = str(args.get("purpose", "agent_observation"))
        if extra_metadata:
            metadata.update(extra_metadata)
        artifact = self.artifact_sink.upload(
            photo, "image/jpeg", metadata, filename="capture.jpg")
        return {
            "artifact": artifact,
            "image": metadata,
            "byte_length": len(photo),
        }

    def get_settings(self, _args=None):
        return {
            "settings": self.driver.get_camera_settings(),
            "specs": self.driver.camera_setting_specs(),
            "saved": self.driver.camera_profile_exists(),
            "safe": self.driver.safe_camera_settings(),
        }

    def apply_temporary(self, args):
        settings = args.get("settings", args)
        self.temporary_dirty = True
        return {"settings": self.driver.apply_camera_settings(settings)}

    def save_settings(self, args):
        """写入正式相机参数（**不再要求维护会话**）。

        为什么去掉 `self.require_maintenance()`：该能力的 `access` 已由 maintenance
        改为 **setup** —— 门槛在 `capabilities.authorize()` 那一层（scope 必须是
        setup/maintenance，**agent 档仍然调不动**）。处理器里再拦一道维护会话，等于把
        新流程（无会话、无现场按键）整条堵死：真机实测 `camera.settings.save`
        （auth_scope=setup）回 `MAINTENANCE_SESSION_REQUIRED "没有有效维护会话"`，
        参数只临时生效、**没落盘**。
        旧流程 `camera.tuning.*` 的会话门槛不受影响（它走会话自己的 `_require`，
        并且直接调 `driver.save_camera_profile`，不经过这里）。
        """
        settings = args.get("settings", args)
        self.temporary_dirty = True
        applied = self.driver.apply_camera_settings(settings)
        saved = self.driver.save_camera_profile(applied)
        self.temporary_dirty = False
        return {"settings": saved, "saved": True}

    def restore_saved(self, _args=None):
        saved = self.driver.load_camera_profile(refresh=True)
        if not saved:
            saved = self.startup_settings or self.driver.safe_camera_settings()
        result = self.driver.apply_camera_settings(saved)
        self.temporary_dirty = False
        return {"settings": result, "saved": True}

    def restore_safe(self, _args=None):
        self.temporary_dirty = True
        return {"settings": self.driver.apply_camera_settings(
            self.driver.safe_camera_settings()), "saved": False}

    def diagnostic_profile(self, _args=None):
        """读「诊断档」的定义 + 设备当前生效值（只读）。

        为什么要这个能力：调试页「原生拍摄」需要一个**固定的、判读友好**的基线档，
        而不是"安全默认"（后者 sharpness 3 / quality 20 / 自动曝光 / 自动增益，
        恰好是最难判读的一档）。

        值只放在设备侧一处（`drivers/camera_manager.diagnostic_camera_settings()`）
        —— 它与镜头/场地/光照相关，云端不必知道具体值，只要"这是诊断档"这个语义。
        """
        return {
            "profile": self.driver.diagnostic_camera_settings(),
            "settings": self.driver.get_camera_settings(),
        }

    def diagnostic_profile_apply(self, _args=None):
        """把相机切到「诊断档」；**不动机械、不写 flash**。

        临时档：与 `apply_temporary` 同口径（`temporary_dirty=True`，断连会被还原），
        调用方拍完应自己 `restore_saved`（云端 `capture_native` 已经这么做）。
        """
        profile = self.driver.diagnostic_camera_settings()
        self.temporary_dirty = True
        return {
            "profile": profile,
            "settings": self.driver.apply_camera_settings(profile),
            "temporary": True,
        }
