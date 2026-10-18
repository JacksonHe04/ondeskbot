"""屏幕 ROI 与机械坐标 v6 标定存储。"""

import config

from . import coordinate_transform as coord
from .compat import epoch_ms
from .errors import CALIBRATION_REQUIRED, DeviceError, INVALID_ARGUMENT
from .storage import load_json, save_json_atomic


CALIBRATION_SCHEMA_VERSION = 1


def _number(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DeviceError(INVALID_ARGUMENT, "{} 必须是数字".format(name))


class CalibrationStore:
    def __init__(self, path=None):
        self.path = path or config.CALIBRATION_FILE
        self.roi = None
        self.roi_revision = 0
        self.roi_source = {}
        self.camera_arm = {
            "x_min": float(config.CAM_ARM_X_MIN),
            "x_max": float(config.CAM_ARM_X_MAX),
            "y_min": float(config.CAM_ARM_Y_MIN),
            "y_max": float(config.CAM_ARM_Y_MAX),
        }
        self.phone = {
            "d_left": float(config.PHONE_D_LEFT),
            "d_right": float(config.PHONE_D_RIGHT),
            "d_top": float(config.PHONE_D_TOP),
            "d_bottom": float(config.PHONE_D_BOTTOM),
        }
        self.load()

    def load(self):
        payload = load_json(self.path, {}) or {}
        if payload.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
            return False
        if payload.get("coordinate_system_version") != coord.COORDINATE_SYSTEM_VERSION:
            return False
        roi = payload.get("roi")
        if roi:
            try:
                self.roi = self._validate_roi(roi)
            except DeviceError:
                self.roi = None
        self.roi_revision = int(payload.get("roi_revision", 0) or 0)
        source = payload.get("roi_source")
        self.roi_source = dict(source) if isinstance(source, dict) else {}
        camera_arm = payload.get("camera_arm")
        if isinstance(camera_arm, dict):
            try:
                self.camera_arm.update(self._validate_camera_arm(camera_arm))
            except DeviceError:
                # camera_arm 只保留兼容诊断；损坏数据不应阻止设备启动。
                pass
        phone = payload.get("phone")
        if isinstance(phone, dict):
            try:
                self.phone.update(self._validate_phone(phone))
            except DeviceError:
                # 用明确无效的几何进入安全门控，禁止静默采用可能错误的默认运动范围。
                self.phone = {
                    "d_left": 0.0, "d_right": 0.0,
                    "d_top": 0.0, "d_bottom": 0.0,
                }
        return True

    def save(self):
        save_json_atomic(self.path, {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "coordinate_system_version": coord.COORDINATE_SYSTEM_VERSION,
            "roi": self.roi,
            "roi_revision": self.roi_revision,
            "roi_source": self.roi_source,
            "camera_arm": self.camera_arm,
            "phone": self.phone,
        })

    def _validate_roi(self, value):
        required = ("x1", "y1", "x2", "y2", "image_width", "image_height")
        if not isinstance(value, dict) or any(name not in value for name in required):
            raise DeviceError(INVALID_ARGUMENT, "ROI 缺少坐标或原图尺寸")
        roi = {name: int(value[name]) for name in required}
        if roi["image_width"] <= 0 or roi["image_height"] <= 0:
            raise DeviceError(INVALID_ARGUMENT, "ROI 原图尺寸无效")
        if not (0 <= roi["x1"] < roi["x2"] <= roi["image_width"]):
            raise DeviceError(INVALID_ARGUMENT, "ROI X 坐标无效")
        if not (0 <= roi["y1"] < roi["y2"] <= roi["image_height"]):
            raise DeviceError(INVALID_ARGUMENT, "ROI Y 坐标无效")
        return roi

    def _validate_camera_arm(self, value):
        result = {}
        for name in ("x_min", "x_max", "y_min", "y_max"):
            result[name] = _number(value.get(name, self.camera_arm[name]), name)
        if not (0 <= result["x_min"] < result["x_max"] <= 1):
            raise DeviceError(INVALID_ARGUMENT, "camera_arm X 范围必须递增且位于 0..1")
        if not (0 <= result["y_min"] < result["y_max"] <= 1):
            raise DeviceError(INVALID_ARGUMENT, "camera_arm Y 范围必须递增且位于 0..1")
        return result

    def _axis_valid(self, phone):
        """phone 的毫米边界在 X / Y 两轴上是否都落在机械可达范围内。

        写入前校验、给云端的机械几何状态、坐标来源标注三处共用同一套判据，
        所以只在这里算一次 —— 分开写迟早会算出两个答案。
        """
        x_ok = coord.axis_geometry_valid(
            phone["d_left"], phone["d_right"],
            config.TOUCH_OFFSET_X_MM, config.ARM_TRAVEL_X)
        y_ok = coord.axis_geometry_valid(
            phone["d_top"], phone["d_bottom"],
            config.TOUCH_OFFSET_Y_MM, config.ARM_TRAVEL_Y)
        return x_ok, y_ok

    def _validate_phone(self, value):
        """校验手机毫米边界；**缺字段或显式 null 都表示保留当前值**。

        契约（对接契约 2.0-draft.5 §4.2）：省略 `phone` 或其中某个字段时，该字段
        沿用设备当前值（从未标定过即出厂默认 20/90/0/144）。因为云端 UI 是"预填
        当前值、用户可只改一项"，所以这里必须支持部分更新；同时也必须支持显式
        `null`（前端把空输入框序列化成 null 是常见做法），否则用户清空一格就会被拒。
        校验不通过一律抛错，**不会静默回退默认值**。
        """
        if not isinstance(value, dict):
            raise DeviceError(INVALID_ARGUMENT, "phone 必须是对象")
        result = {}
        for name in ("d_left", "d_right", "d_top", "d_bottom"):
            raw = value.get(name)
            if raw is None:
                raw = self.phone[name]
            result[name] = _number(raw, name)
        if result["d_right"] <= result["d_left"]:
            raise DeviceError(INVALID_ARGUMENT, "手机左右边界必须满足 left < right")
        if result["d_bottom"] <= result["d_top"]:
            raise DeviceError(INVALID_ARGUMENT, "手机上下边界必须满足 top < bottom")
        x_ok, y_ok = self._axis_valid(result)
        if not x_ok:
            raise DeviceError(INVALID_ARGUMENT, "手机左右边界超出机械 X 可达范围")
        if not y_ok:
            raise DeviceError(INVALID_ARGUMENT, "手机上下边界超出机械 Y 可达范围")
        return result

    def get_public(self):
        return {
            "coordinate_system_version": coord.COORDINATE_SYSTEM_VERSION,
            "roi": self.roi,
            "roi_status": self.roi_status(),
            "roi_revision": self.roi_revision,
            "roi_source": dict(self.roi_source),
            "camera_arm": dict(self.camera_arm),
            "phone": dict(self.phone),
            "axis_source": self.axis_source(),
            "motion_geometry": self.motion_geometry_status(),
        }

    def set_roi(self, value, source=None):
        self.roi = self._validate_roi(value)
        self.roi_revision += 1
        source = source if isinstance(source, dict) else {}
        self.roi_source = {
            "method": str(source.get("method", "cloud_or_maintenance")),
            "session_id": str(source.get("session_id", "")),
            "artifact_id": str(source.get("artifact_id", "")),
            "updated_at_ms": epoch_ms(),
        }
        self.save()
        return dict(self.roi)

    def validate_roi(self, value):
        return self._validate_roi(value)

    def clear_roi(self):
        self.roi = None
        self.roi_revision += 1
        self.roi_source = {"method": "cleared", "updated_at_ms": epoch_ms()}
        self.save()

    def roi_status(self, image_width=None, image_height=None):
        if not self.roi:
            return "required"
        if (image_width is not None and image_height is not None and
                (self.roi["image_width"] != int(image_width) or
                 self.roi["image_height"] != int(image_height))):
            return "required"
        return "ready"

    def set_roi_and_geometry(self, roi, phone=None, source=None):
        """ROI 与手机毫米边界在**同一次 save()** 里落盘。

        云端框选向导把像素矩形和 `d_*` 放在**同一次 commit** 里提交，契约要求
        "ROI 与 phone 在同一次保存里落盘，不存在只写入一半的情况"。所以这里：

        1. **先全部校验**（`_validate_roi` / `_validate_phone`）—— 任一项不通过就
           直接抛错，`self.roi` / `self.phone` **一个字节都没动**；
        2. 校验全过才赋值；
        3. **只调一次 `save()`** → 文件层面只有一次原子替换，不存在中间态。

        `phone=None` 表示"这次不提供 phone"，几何保持当前值（契约里的合法用法）。
        """
        validated_roi = self._validate_roi(roi)
        validated_phone = None
        if phone is not None:
            validated_phone = self._validate_phone(phone)

        self.roi = validated_roi
        self.roi_revision += 1
        src = source if isinstance(source, dict) else {}
        self.roi_source = {
            "method": str(src.get("method", "cloud_setup")),
            "session_id": str(src.get("session_id", "")),
            "artifact_id": str(src.get("artifact_id", "")),
            "updated_at_ms": epoch_ms(),
        }
        if validated_phone is not None:
            self.phone.update(validated_phone)
        self.save()
        return {"roi": dict(self.roi), "phone": dict(self.phone),
                "roi_revision": self.roi_revision}

    def set_geometry(self, camera_arm=None, phone=None):
        if camera_arm is not None:
            self.camera_arm = self._validate_camera_arm(camera_arm)
        if phone is not None:
            self.phone = self._validate_phone(phone)
        self.save()
        return self.get_public()

    def require_roi(self):
        if not self.roi:
            raise DeviceError(CALIBRATION_REQUIRED, "屏幕 ROI 尚未标定")
        return self.roi

    def motion_geometry_status(self):
        x_ok, y_ok = self._axis_valid(self.phone)
        return {
            "status": "ready" if x_ok and y_ok else "required",
            "x_valid": x_ok,
            "y_valid": y_ok,
        }

    def require_motion_geometry(self):
        status = self.motion_geometry_status()
        if status["status"] != "ready":
            raise DeviceError(
                CALIBRATION_REQUIRED,
                "机械几何无效，禁止使用比例坐标运动",
                status,
            )
        return status

    def require_task_ready(self, image_width, image_height):
        if self.roi_status(image_width, image_height) != "ready":
            raise DeviceError(CALIBRATION_REQUIRED, "云端屏幕 ROI 尚未完成或已失效", {
                "roi_status": self.roi_status(image_width, image_height),
                "expected_image_width": int(image_width),
                "expected_image_height": int(image_height),
            })
        self.require_motion_geometry()
        return {"roi_status": "ready", "motion_geometry_status": "ready"}

    def axis_source(self):
        x_ok, y_ok = self._axis_valid(self.phone)
        return {"x": "phone_mm" if x_ok else "invalid",
                "y": "phone_mm" if y_ok else "invalid"}

    def screen_to_motor(self, screen_x, screen_y, click_offset=False):
        self.require_motion_geometry()
        sx = coord.clamp01(screen_x)
        sy = coord.clamp01(screen_y)
        touch_x, touch_y = coord.screen_ratio_to_touch_mm(
            sx, sy, self.phone["d_left"], self.phone["d_right"],
            self.phone["d_top"], self.phone["d_bottom"])
        mx, my = coord.touch_mm_to_motor_ratio(
            touch_x, touch_y, config.ARM_TRAVEL_X, config.ARM_TRAVEL_Y,
            config.TOUCH_OFFSET_X_MM, config.TOUCH_OFFSET_Y_MM)

        if click_offset:
            mx += float(config.CLICK_TARGET_OFFSET_X_MM) / float(config.ARM_TRAVEL_X)
            my += float(config.CLICK_TARGET_OFFSET_Y_MM) / float(config.ARM_TRAVEL_Y)

        margin = float(config.COORD_MARGIN)
        mx = max(margin, min(1.0 - margin, mx))
        my = max(margin, min(1.0 - margin, my))
        if config.COORD_INVERT_X:
            mx = 1.0 - mx
        if config.COORD_INVERT_Y:
            my = 1.0 - my
        return mx, my

    def photo_metadata(self, width, height):
        roi = self.roi
        if roi and (roi["image_width"] != int(width) or roi["image_height"] != int(height)):
            roi = None
        return {
            "image_width": int(width),
            "image_height": int(height),
            "coordinate_system_version": coord.COORDINATE_SYSTEM_VERSION,
            "orientation": {"rotation": 0, "hmirror": False, "vflip": False},
            "screen_roi": roi,
        }
