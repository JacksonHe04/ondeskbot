"""从 OnDeskBot 1.x 一次性迁移非 Agent 配置。"""

import config

from . import coordinate_transform as coord
from .calibration import CALIBRATION_SCHEMA_VERSION
from .storage import load_json, save_json_atomic


def _legacy_v6(payload):
    return isinstance(payload, dict) and int(payload.get("coord_version", -1)) == 6


def migrate_legacy_files():
    existing = load_json(config.MIGRATION_FILE, None)
    if existing:
        return existing

    report = {
        "version": 1,
        "wifi": False,
        "roi": False,
        "camera_arm": False,
        "phone": False,
        "camera_profile": False,
        "excluded": ["llm_config", "prompts", "agent_state", "task_history"],
    }

    if load_json(config.WIFI_CRED_FILE, None) is None:
        wifi = load_json(config.LEGACY_WIFI_CRED_FILE, None)
        if isinstance(wifi, dict) and str(wifi.get("ssid", "")).strip():
            save_json_atomic(config.WIFI_CRED_FILE, {
                "ssid": str(wifi["ssid"]),
                "password": str(wifi.get("password", "")),
            })
            report["wifi"] = True

    if load_json(config.CALIBRATION_FILE, None) is None:
        roi_old = load_json(config.LEGACY_SCREEN_ROI_FILE, None)
        arm_old = load_json(config.LEGACY_CAM_ARM_FILE, None)
        phone_old = load_json(config.LEGACY_PHONE_POS_FILE, None)
        calibration = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "coordinate_system_version": 6,
            "roi": None,
            "camera_arm": {
                "x_min": config.CAM_ARM_X_MIN, "x_max": config.CAM_ARM_X_MAX,
                "y_min": config.CAM_ARM_Y_MIN, "y_max": config.CAM_ARM_Y_MAX,
            },
            "phone": {
                "d_left": config.PHONE_D_LEFT, "d_right": config.PHONE_D_RIGHT,
                "d_top": config.PHONE_D_TOP, "d_bottom": config.PHONE_D_BOTTOM,
            },
        }
        if _legacy_v6(roi_old):
            iw = int(roi_old.get("iw", 0) or 0)
            ih = int(roi_old.get("ih", 0) or 0)
            candidate = {
                "x1": int(roi_old.get("x1", -1)),
                "y1": int(roi_old.get("y1", -1)),
                "x2": int(roi_old.get("x2", -1)),
                "y2": int(roi_old.get("y2", -1)),
                "image_width": iw,
                "image_height": ih,
            }
            if (iw > 0 and ih > 0 and
                    0 <= candidate["x1"] < candidate["x2"] <= iw and
                    0 <= candidate["y1"] < candidate["y2"] <= ih):
                calibration["roi"] = candidate
                report["roi"] = True
        if _legacy_v6(arm_old):
            values = {
                "x_min": float(arm_old.get("x_min", config.CAM_ARM_X_MIN)),
                "x_max": float(arm_old.get("x_max", config.CAM_ARM_X_MAX)),
                "y_min": float(arm_old.get("y_min", config.CAM_ARM_Y_MIN)),
                "y_max": float(arm_old.get("y_max", config.CAM_ARM_Y_MAX)),
            }
            if (0 <= values["x_min"] < values["x_max"] <= 1 and
                    0 <= values["y_min"] < values["y_max"] <= 1):
                calibration["camera_arm"] = values
                report["camera_arm"] = True
        if _legacy_v6(phone_old):
            values = {
                "d_left": float(phone_old.get("d_left", config.PHONE_D_LEFT)),
                "d_right": float(phone_old.get("d_right", config.PHONE_D_RIGHT)),
                "d_top": float(phone_old.get("d_top", config.PHONE_D_TOP)),
                "d_bottom": float(phone_old.get("d_bottom", config.PHONE_D_BOTTOM)),
            }
            if (coord.axis_geometry_valid(
                    values["d_left"], values["d_right"],
                    config.TOUCH_OFFSET_X_MM, config.ARM_TRAVEL_X) and
                    coord.axis_geometry_valid(
                        values["d_top"], values["d_bottom"],
                        config.TOUCH_OFFSET_Y_MM, config.ARM_TRAVEL_Y)):
                calibration["phone"] = values
                report["phone"] = True
        save_json_atomic(config.CALIBRATION_FILE, calibration)

    if load_json(config.CAM_PROFILE_FILE, None) is None:
        profile = load_json(config.LEGACY_CAM_PROFILE_FILE, None)
        if isinstance(profile, dict) and profile.get("version") == 1:
            settings = profile.get("settings")
            if isinstance(settings, dict) and settings:
                # camera_manager 会在加载时再次执行完整白名单与范围校验。
                save_json_atomic(config.CAM_PROFILE_FILE, profile)
                report["camera_profile"] = True

    save_json_atomic(config.MIGRATION_FILE, report)
    return report
