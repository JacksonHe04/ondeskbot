"""云端能力目录 v1：描述参数，不替代原硬件安全校验。"""
import config

SCHEMA_VERSION = 1


def field(kind, **kwargs):
    result = {"type": kind}
    result.update(kwargs)
    return result


def parameters(name):
    string = field("string", minLength=1)
    ratio = field("number", minimum=0, maximum=1, unit="screen_ratio")
    session = {"session_id": string}
    settings = field("object", additionalProperties=False, properties={
        k: field("integer", minimum=lo, maximum=hi) for k, lo, hi in (
            ("quality", 10, config.CAM_TUNE_MAX_JPEG_QUALITY), ("brightness", -2, 2),
            ("contrast", -2, 2), ("saturation", -2, 2), ("sharpness", -3, 3),
            ("denoise", 0, 8), ("aec_value", 0, 1200), ("ae_level", -2, 2),
            ("agc_gain", 0, 30), ("wb_mode", 0, 4))},
        description="支持部分更新；省略字段保留当前值；另须满足 camera.settings.get.setting_specs")
    for k in ("exposure_ctrl", "aec2", "gain_ctrl", "whitebal", "awb_gain"):
        settings["properties"][k] = field("boolean")
    roi = field("object", required=["x1", "y1", "x2", "y2", "image_width", "image_height"],
                properties={k: field("integer", minimum=0, unit="pixel") for k in
                            ("x1", "y1", "x2", "y2", "image_width", "image_height")},
                description="0<=x1<x2<=image_width，0<=y1<y2<=image_height，尺寸须匹配原图")
    # 手机屏幕的绝对毫米边界。`calibration.roi.commit` 与 `calibration.geometry.set`
    # 共用同一形状，保证两条写入路径的字段与校验一致。
    # 只声明类型与单位，**不在这里划范围** —— 机械可达性由设备端 `_validate_phone`
    # 按真实行程权威校验，schema 里的粗范围只会重复且容易脱节。
    phone_field = field(
        "object",
        properties={k: field("number", unit="mm")
                    for k in ("d_left", "d_right", "d_top", "d_bottom")},
        description=("手机屏幕毫米边界，从机械 Home 起算；须满足 d_left<d_right、"
                     "d_top<d_bottom 且在机械可达范围内。省略字段、省略整个 phone、"
                     "或把字段写成 null，都表示保留设备当前值"))
    properties, required = {}, []
    if name == "device.capabilities.list":
        properties = {"offset": field("integer", minimum=0, default=0),
                      "limit": field("integer", minimum=1, maximum=4, default=2),
                      "expected_digest": string}
    elif name == "command.result.get":
        properties, required = {"request_id": string}, ["request_id"]
    elif name in ("motion.tap", "motion.double_tap", "motion.long_press"):
        properties, required = {"x": ratio, "y": ratio}, ["x", "y"]
        if name == "motion.long_press":
            properties["duration_ms"] = field("integer", minimum=500, maximum=5000,
                                              default=config.LONG_PRESS_HOLD_MS, unit="ms")
    elif name == "motion.swipe":
        properties, required = {"direction": field("string", enum=["up", "down", "left", "right"])}, ["direction"]
    elif name == "motion.raw_move":
        properties = {"x_steps": field("integer", minimum=0, maximum=config.RAW_MOVE_MAX_X_STEPS, unit="step"),
                      "y_steps": field("integer", minimum=0, maximum=config.RAW_MOVE_MAX_Y_STEPS, unit="step")}
        required = ["x_steps", "y_steps"]
    elif name == "motion.raw_z_click":
        properties = {"count": field("integer", enum=[1, 2], default=1),
                      "hold_ms": field("integer", minimum=20, maximum=5000, default=config.Z_CLICK_HOLD_MS, unit="ms")}
    elif name == "camera.capture":
        properties = {"max_attempts": field("integer", minimum=1, maximum=3, default=3),
                      "purpose": field("string", default="agent_observation")}
    elif name == "camera.debug_capture":
        # 调试页「拍照」，调焦用。与 camera.capture 同形参数，
        # 但**不要求任务与 ROI**（调焦阶段 ROI 还没标定）。
        properties = {"max_attempts": field("integer", minimum=1, maximum=3, default=3),
                      "purpose": field("string", default="debug_focus")}
    elif name in ("camera.settings.apply_temporary", "camera.settings.save"):
        properties, required = {"settings": settings}, ["settings"]
    elif name.startswith("camera.tuning.") and name != "camera.tuning.status":
        properties, required = dict(session), ["session_id"]
        if name in ("camera.tuning.sample", "camera.tuning.commit"):
            properties["settings"] = settings
            required.append("settings")
    elif name in ("calibration.roi.begin", "calibration.roi.cancel", "calibration.roi.commit"):
        properties, required = dict(session), ["session_id"]
        if name == "calibration.roi.commit":
            properties.update(source_artifact_id=string, roi=roi,
                              orientation=field("object", description="原样回传 begin 照片元数据中的 orientation"),
                              coordinate_system_version=field("integer", enum=[config.COORDINATE_SYSTEM_VERSION]),
                              # 云端框选向导在同一次 commit 里同时提交像素矩形与手机毫米边界，
                              # 由设备原子保存。
                              # **可选**：省略整个 phone、省略其中某个字段、或把字段写成
                              # null，都表示"该字段保留设备当前值"，不因此拒绝。
                              phone=phone_field)
            required += ["source_artifact_id", "roi", "orientation", "coordinate_system_version"]
    elif name == "calibration.roi.set":
        properties, required = {"roi": roi}, ["roi"]
    elif name == "calibration.geometry.set":
        properties = {"camera_arm": field("object", properties={k: ratio for k in ("x_min", "x_max", "y_min", "y_max")}),
                      "phone": phone_field}
    elif name in ("task.begin", "task.cancel"):
        properties = {"task_id": field("string", description="优先使用命令信封 task_id；两处至少提供一处")}
        if name == "task.begin":
            properties["confirm_timeout_s"] = field("integer", default=config.TASK_CONFIRM_TIMEOUT_S, unit="s",
                                                   description="省略或0使用默认值")
    elif name == "task.confirm":
        # 云端确认流：**无入参**（task_id 只走命令信封，与 task.end 同口径）。
        # 语义等价本地按键确认：把 pending 的任务转 active；两条路径先到先确认。
        properties, required = {}, []
    elif name in ("maintenance.session.request", "maintenance.session.close"):
        properties = {"session_id": field("string", description="request 省略时取命令 request_id；close 省略时关闭当前会话")}
        if name == "maintenance.session.request":
            properties["confirm_timeout_s"] = field("integer", default=config.MAINTENANCE_CONFIRM_TIMEOUT_S, unit="s")
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def canonical_manifest(value):
    """固定 ASCII JSON：排序键、无空白、非 ASCII 用小写 \\u；仅允许整数。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        result = '"'
        escapes = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\f': '\\f', '\n': '\\n', '\r': '\\r', '\t': '\\t'}
        for ch in value:
            n = ord(ch)
            if ch in escapes:
                result += escapes[ch]
            elif 32 <= n <= 126:
                result += ch
            elif n <= 65535:
                result += "\\u%04x" % n
            else:
                n -= 65536
                result += "\\u%04x\\u%04x" % (55296 + (n >> 10), 56320 + (n & 1023))
        return result + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_manifest(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(canonical_manifest(k) + ":" + canonical_manifest(value[k]) for k in sorted(value)) + "}"
    raise ValueError("manifest only supports integer JSON numbers")
