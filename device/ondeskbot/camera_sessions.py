"""云端编排的两条相机会话：ROI 标定与调参。

两条会话的状态机形状一样 —— 谁开会话、谁在结束时把临时参数收拾回去，
所以 session_id 校验与回滚只写一遍（`_SessionBase`），子类只做各自的事。
"""

import config

from .compat import ticks_add, ticks_diff, ticks_ms
from .log import log
from .errors import (
    CALIBRATION_REQUIRED,
    CAMERA_CAPTURE_FAILED,
    DeviceError,
    INVALID_ARGUMENT,
)


class _SessionBase:
    """会话公共骨架：session_id 校验、超时判定与临时参数回滚。"""

    LABEL = "会话"

    def __init__(self, photo_service, now_provider=None):
        self.photo = photo_service
        self._now = now_provider or ticks_ms
        self.session_id = ""
        self.deadline_ms = 0

    @property
    def active(self):
        return bool(self.session_id)

    def _expired(self):
        return ticks_diff(self._now(), self.deadline_ms) >= 0

    def _require_session_id(self, args):
        """校验调用方带来的 session_id。

        把"没带 session_id"与"带了但不对"**分开报** —— 前者是**调用姿势**问题，
        让调用方一眼知道该补什么，而不是去猜"会话是不是没了"。
        """
        requested = str(args.get("session_id", ""))
        if not requested:
            if not self.active:
                raise DeviceError(INVALID_ARGUMENT,
                                  "没有进行中的{}".format(self.LABEL))
            raise DeviceError(INVALID_ARGUMENT,
                              "必须带 session_id（当前会话为 {}）".format(self.session_id))
        if not self.active or requested != self.session_id:
            raise DeviceError(INVALID_ARGUMENT,
                              "{}不存在或 session_id 不匹配".format(self.LABEL))

    def _rollback_temporary(self):
        """会话结束时把临时相机参数回滚到正式档。

        谁开会话、谁在结束时收拾 —— 不要靠"没有活动维护会话就回滚"那种全局判据，
        那条会把**无会话写入**（新调参流程）一并掀掉。
        """
        if getattr(self.photo, "temporary_dirty", False):
            try:
                self.photo.restore_saved({})
            except Exception as error:
                log("[相机] 会话结束时回滚临时参数失败: {}".format(error))


class RoiCalibrationSession(_SessionBase):
    """云端 ROI 标定会话；设备负责拍照、尺寸校验和原子保存。"""

    LABEL = "ROI 标定会话"

    def __init__(self, photo_service, calibration, now_provider=None):
        _SessionBase.__init__(self, photo_service, now_provider)
        self.calibration = calibration
        self.artifact_id = ""
        self.frame = None

    def _clear(self):
        self.session_id = ""
        self.deadline_ms = 0
        self.artifact_id = ""
        self.frame = None
        self._rollback_temporary()

    def _require(self, args):
        self._require_session_id(args)
        if self._expired():
            expired = self.session_id
            self._clear()
            raise DeviceError(INVALID_ARGUMENT, "ROI 标定会话已超时", {
                "session_id": expired,
            })

    def begin(self, args, trace_context=None):
        if self.active:
            raise DeviceError(INVALID_ARGUMENT, "已有 ROI 标定会话正在运行")
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise DeviceError(INVALID_ARGUMENT, "session_id 不能为空")
        self.session_id = session_id
        self.deadline_ms = ticks_add(self._now(), int(
            config.ROI_CALIBRATION_SESSION_TTL_S) * 1000)
        try:
            capture = self.photo.capture(
                {"purpose": "roi_calibration", "max_attempts": 3},
                {"roi_calibration_session_id": session_id,
                 "trace": trace_context or {}})
            artifact = capture.get("artifact", {})
            image = capture.get("image", {})
            self.artifact_id = str(artifact.get("artifact_id", ""))
            self.frame = {
                "coordinate_system_version": config.COORDINATE_SYSTEM_VERSION,
                "image_width": int(image.get("image_width", 0)),
                "image_height": int(image.get("image_height", 0)),
                "orientation": image.get("orientation", {}),
            }
            if (not self.artifact_id or self.frame["image_width"] <= 0 or
                    self.frame["image_height"] <= 0):
                raise DeviceError(
                    CAMERA_CAPTURE_FAILED,
                    "ROI 标定照片缺少 artifact 或原图尺寸",
                )
            return {
                "session_id": session_id,
                "deadline_ms": self.deadline_ms,
                "artifact": artifact,
                "frame": dict(self.frame),
                "current_roi": self.calibration.roi,
                "roi_revision": self.calibration.roi_revision,
                # 云端框选向导用 current_phone 预填四个毫米输入框。
                # 从未标定过时即出厂默认 20/90/0/144。
                "current_phone": dict(self.calibration.phone),
            }
        except Exception:
            self._clear()
            raise

    def commit(self, args):
        self._require(args)
        artifact_id = str(args.get("source_artifact_id", ""))
        if artifact_id != self.artifact_id:
            raise DeviceError(INVALID_ARGUMENT, "source_artifact_id 与标定照片不匹配")
        roi = self.calibration.validate_roi(args.get("roi"))
        if (roi["image_width"] != self.frame["image_width"] or
                roi["image_height"] != self.frame["image_height"]):
            raise DeviceError(INVALID_ARGUMENT, "ROI 原图尺寸与标定照片不匹配")
        if "orientation" not in args:
            raise DeviceError(INVALID_ARGUMENT, "orientation 不能为空")
        orientation = args.get("orientation")
        if orientation != self.frame["orientation"]:
            raise DeviceError(INVALID_ARGUMENT, "ROI orientation 与标定照片不匹配")
        if args.get("coordinate_system_version") != config.COORDINATE_SYSTEM_VERSION:
            raise DeviceError(INVALID_ARGUMENT, "coordinate_system_version 不匹配")
        session_id = self.session_id
        # ROI 与 phone 在**同一次 save()** 里落盘：set_roi_and_geometry 先校验两者、
        # 再赋值、最后只写一次文件。任何一项校验失败都会在赋值之前抛错，所以旧 ROI
        # 与旧几何都不动；也不会出现"只写入一半"的中间态。
        saved = self.calibration.set_roi_and_geometry(roi, args.get("phone"), {
            "method": "cloud_setup",
            "session_id": session_id,
            "artifact_id": self.artifact_id,
        })
        revision = saved["roi_revision"]
        self._clear()
        return {
            "session_id": session_id,
            "roi": saved["roi"],
            "phone": saved["phone"],
            "roi_revision": revision,
            "roi_status": "ready",
            "verification_required": True,
        }

    def cancel(self, args):
        self._require(args)
        session_id = self.session_id
        self._clear()
        return {"session_id": session_id, "cancelled": True}

    def verify(self, args=None, trace_context=None):
        """拍一张**新的**验证图并带上刚保存的 ROI 与当前几何。

        契约要求：`purpose=roi_calibration_verification`，元数据带 `roi_revision`、
        `screen_roi` 和**当前 phone**；并且"若本次 commit 改动了 phone，验证图必须
        重新拍一次，不得复用 commit 前的照片"。这里每次调用都走完整拍照流程，
        **结构上不存在复用旧图的路径** —— 不需要额外的标志位来"记住要重拍"。
        """
        width, height = self.photo.driver.get_frame_size()
        if self.calibration.roi_status(width, height) != "ready":
            raise DeviceError(CALIBRATION_REQUIRED, "ROI 尚未完成或与当前图像尺寸不匹配")
        return self.photo.capture(
            {"purpose": "roi_calibration_verification", "max_attempts": 3},
            {"roi_revision": self.calibration.roi_revision,
             "phone": dict(self.calibration.phone),
             "trace": trace_context or {}})

    def expire_if_needed(self):
        if self.active and self._expired():
            session_id = self.session_id
            self._clear()
            return {"session_id": session_id, "expired": True}
        return None

    def snapshot(self, _args=None):
        return {
            "active": self.active,
            "session_id": self.session_id,
            "deadline_ms": self.deadline_ms,
            "source_artifact_id": self.artifact_id,
            "frame": self.frame,
            "roi_status": self.calibration.roi_status(),
            "roi_revision": self.calibration.roi_revision,
            # status 也要给当前 phone 与机械几何，云端不必再发一条 calibration.get
            # 就能显示"现在生效的边界是什么"。
            "phone": dict(self.calibration.phone),
            "motion_geometry": self.calibration.motion_geometry_status(),
        }


class CameraTuningSession(_SessionBase):
    """设备只负责参数原子操作；ROI 评分与候选搜索由云端完成。"""

    LABEL = "调参会话"

    def __init__(self, photo_service, now_provider=None, runtime=None):
        _SessionBase.__init__(self, photo_service, now_provider)
        self.original_settings = None
        self.runtime = runtime

    def _require(self, args):
        self._require_session_id(args)
        if self.runtime:
            self.runtime.require_maintenance()
        if self._expired():
            self.cancel({"session_id": self.session_id})
            raise DeviceError(INVALID_ARGUMENT, "调参会话已超时并恢复原参数")

    def _require_without_expiry(self, args):
        """cancel 走这条口：不再判超时，否则会话一超时就永远取消不掉。"""
        self._require_session_id(args)

    def begin(self, args):
        if self.active:
            raise DeviceError(INVALID_ARGUMENT, "已有调参会话正在运行")
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise DeviceError(INVALID_ARGUMENT, "session_id 不能为空")
        self.session_id = session_id
        self.original_settings = (self.photo.driver.load_camera_profile(refresh=True) or
                                  self.photo.startup_settings or self.photo.driver.get_camera_settings())
        if self.photo.startup_settings is None:
            self.photo.startup_settings = dict(self.original_settings)
        self.deadline_ms = ticks_add(self._now(), int(
            config.CAMERA_TUNING_SESSION_TTL_S) * 1000)
        if self.runtime:
            self.runtime.require_maintenance()
            remaining = max(0, ticks_diff(self.runtime.maintenance_deadline_ms, self._now()))
            self.deadline_ms = ticks_add(self._now(), min(
                remaining, config.CAMERA_TUNING_SESSION_TTL_S * 1000))
        width, height = self.photo.driver.get_frame_size()
        if self.photo.calibration.roi_status(width, height) != "ready":
            self.cancel({"session_id": self.session_id})
            raise DeviceError(
                CALIBRATION_REQUIRED,
                "ROI 尚未完成或与当前图像尺寸不匹配，不能开始调参",
            )
        return {
            "session_id": self.session_id,
            "original_settings": dict(self.original_settings),
            "safe_settings": self.photo.driver.safe_camera_settings(),
            "setting_specs": self.photo.driver.camera_setting_specs(),
            "roi": self.photo.calibration.require_roi(),
            "deadline_ms": self.deadline_ms,
        }

    def sample(self, args, trace_context=None):
        self._require(args)
        settings = args.get("settings")
        if not isinstance(settings, dict):
            raise DeviceError(INVALID_ARGUMENT, "settings 必须是对象")
        self.photo.temporary_dirty = True
        applied = self.photo.driver.apply_camera_settings(settings)
        self._require(args)
        return self.photo.capture(
            {"purpose": "camera_tuning", "max_attempts": 3},
            {"tuning_session_id": self.session_id,
             "candidate_settings": applied,
             "trace": trace_context or {}})

    def commit(self, args):
        self._require(args)
        settings = args.get("settings")
        if not isinstance(settings, dict):
            raise DeviceError(INVALID_ARGUMENT, "settings 必须是对象")
        self.photo.temporary_dirty = True
        applied = self.photo.driver.apply_camera_settings(settings)
        self._require(args)
        saved = self.photo.driver.save_camera_profile(applied)
        self.photo.temporary_dirty = False
        session_id = self.session_id
        self._clear()
        return {"session_id": session_id, "settings": saved, "saved": True}

    def cancel(self, args):
        self._require_without_expiry(args)
        restored = self.photo.restore_saved({})["settings"]
        session_id = self.session_id
        self._clear()
        return {"session_id": session_id, "settings": restored, "restored": True}

    def expire_if_needed(self):
        if self.active and (self._expired() or
                            (self.runtime and (not self.runtime.connected or
                             self.runtime.maintenance_status != "active"))):
            return self.cancel({"session_id": self.session_id})
        return None

    def _clear(self):
        self.session_id = ""
        self.original_settings = None
        self.deadline_ms = 0
        # 会话结束自己回滚临时参数（commit 路径已把 temporary_dirty 置 False，
        # 这里不会重复写）
        self._rollback_temporary()

    def snapshot(self, _args=None):
        return {"active": self.active, "session_id": self.session_id,
                "deadline_ms": self.deadline_ms}
