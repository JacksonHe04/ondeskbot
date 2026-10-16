"""
摄像头管理模块 (OV2640 / OV3660 / OV5640)

支持三种 MicroPython camera 固件 API（按优先级）：
  1. cnadler86 Camera 类  — from camera import Camera (推荐，支持 OV3660)
  2. kaki5 camera 模块    — import camera; camera.init()
  3. esp32_camera 模块    — from esp32_camera import Camera

引脚定义适用于 Freenove ESP32-S3-WROOM CAM 开发板。

OV3660 特殊处理：
  必须先以默认参数(RGB565)初始化，再 重配置 切换到 JPEG 模式，
  最后 set_frame_size 调整分辨率。直接以 JPEG 初始化会导致 capture 失败。
  经测试 OV3660 + cnadler86 固件仅 SVGA(800x600) 和 XGA(1024x768) 可用。

OV5640 JPEG 策略：
  本版本强制恢复摄像头硬件 JPEG 输出。若 JPEG 验证失败，直接报告失败，
  不再降级到 RGB565/BMP，避免长期运行时高 CPU 占用和内存碎片。
"""

import gc
import time
import struct
import ubinascii
import json
import os
import config
from ondeskbot import cooperative
from ondeskbot.log import log


_cam_type = None        # "cnadler86" | "kaki5" | "esp32_camera" | None
_cam_ready = False
_cam_obj = None         # cnadler86 Camera 实例
_jpeg_broken = False    # JPEG 不可用时为 True；CAM_FORCE_JPEG=True 时会直接失败
_jpeg_w = 0
_jpeg_h = 0
_capture_count_since_restart = 0
_last_capture_ms = 0
_restart_guard = False
_consecutive_capture_failures = 0
_saved_profile_cache = False  # False=尚未读取，None=无有效文件，dict=已验证配置
_active_settings = {}


CAMERA_PROFILE_VERSION = 1

# 仅开放不会改变图像几何关系的传感器参数。方向、分辨率、像素格式和时钟
# 不在白名单中，调参页面和保存文件均无法修改它们。
_SETTING_SPECS = {
    "quality": ("int", 10, int(getattr(config, "CAM_TUNE_MAX_JPEG_QUALITY", 20))),
    "brightness": ("int", -2, 2),
    "contrast": ("int", -2, 2),
    "saturation": ("int", -2, 2),
    "sharpness": ("int", -3, 3),
    "denoise": ("int", 0, 8),
    "exposure_ctrl": ("bool", 0, 1),
    "aec_value": ("int", 0, 1200),
    "ae_level": ("int", -2, 2),
    "aec2": ("bool", 0, 1),
    "gain_ctrl": ("bool", 0, 1),
    "agc_gain": ("int", 0, 30),
    "whitebal": ("bool", 0, 1),
    "awb_gain": ("bool", 0, 1),
    "wb_mode": ("int", 0, 4),
}


def safe_camera_settings():
    """返回调参页面的安全推荐值；调用者可自由修改返回的副本。"""
    return {
        "quality": int(getattr(config, "CAM_TUNE_SAFE_JPEG_QUALITY", 20)),
        "brightness": 0,
        "contrast": 1,
        "saturation": 0,
        "sharpness": 3,
        "denoise": 0,
        "exposure_ctrl": True,
        "aec_value": 300,
        "ae_level": 0,
        "aec2": False,
        "gain_ctrl": True,
        "agc_gain": 0,
        "whitebal": True,
        "awb_gain": True,
        "wb_mode": 0,
    }


def diagnostic_camera_settings():
    """「诊断档」：拍诊断/基线照片时用的**固定一组值**，与当前正式档无关。

    为什么需要（wings 群 seq 122 服务端提、设备侧实现）：

    调试页「原生拍摄」原先先落到 `safe_camera_settings()`（`sharpness 3 / quality 20 /
    自动曝光 / 自动增益`）—— 那是**调参页的推荐起点**，不是一个能判读的档：
    按同一张参数说明表，它在每个维度上都朝"更难判读"走（压得最狠、加了锐、
    曝光与增益都挂在自动环上）。而它本来是要用来判"清不清晰"的基线图，于是
    "看到哪里都软"这件事里，有一半是这一档自己造成的。

    诊断档反过来取**判读友好**的那组 —— 就是现场调焦在用的、也是 2026-10-06 00:11
    写进正式档的那组：锁曝光（`exposure_ctrl=False` + `aec_value`）、锁增益
    （`gain_ctrl=False` + `agc_gain`）、不加锐（`sharpness=-3`）、小字清楚（`quality=10`）。
    它仍是"原生/基线"—— **与上一轮调过的参数无关、每轮都一样**，所以跨轮可比。

    **值放在设备侧一处**：这组与镜头/场地/光照相关（换镜头、重新对焦、换场地都要改），
    云端不必知道、也不必同步；要改就改这里，两边不会失配。
    """
    return {
        "quality": 10,
        "brightness": 0,
        "contrast": 1,
        "saturation": 0,
        "sharpness": -3,
        "denoise": 0,
        "exposure_ctrl": False,
        "aec_value": 550,
        "ae_level": 0,
        "aec2": False,
        "gain_ctrl": False,
        "agc_gain": 2,
        "whitebal": True,
        "awb_gain": True,
        "wb_mode": 0,
    }


def camera_setting_specs():
    """返回网页可调参数及其类型/范围的副本。

    值必须是 **list** 而不是 tuple。响应会被写进持久化命令日志
    （`ondeskbot/reliability.py: CommandJournal` → `DurableRecord.save`），
    而后者落盘后要做读回比对；tuple 经 JSON 往返会变成 list，`!=` 必然成立，
    于是被误判成"写失败"，并把整条记录**永久锁死**（之后所有带副作用的命令
    都回 `STORAGE_RECOVERY_REQUIRED`）。真机 2026-09-26 13:06 第一次调参时踩到。
    """
    return {name: list(spec) for name, spec in _SETTING_SPECS.items()}


def _normalize_camera_settings(values):
    if not isinstance(values, dict):
        raise ValueError("摄像头参数必须是对象")
    normalized = {}
    for name, value in values.items():
        spec = _SETTING_SPECS.get(name)
        if spec is None:
            raise ValueError("不允许修改参数: {}".format(name))
        kind, minimum, maximum = spec
        if kind == "bool":
            if isinstance(value, bool):
                normalized[name] = value
            elif value in (0, 1, "0", "1"):
                normalized[name] = bool(int(value))
            else:
                raise ValueError("{} 必须为布尔值".format(name))
            continue
        try:
            number = int(value)
        except Exception:
            raise ValueError("{} 必须为整数".format(name))
        if number < minimum or number > maximum:
            raise ValueError("{} 超出范围 {}..{}".format(name, minimum, maximum))
        normalized[name] = number
    return normalized


def _profile_path():
    return getattr(config, "CAM_PROFILE_FILE", "/camera_profile.json")


def load_camera_profile(refresh=False):
    """读取并验证已保存的正式参数；无有效文件时返回 None。"""
    global _saved_profile_cache
    if not refresh and _saved_profile_cache is not False:
        return dict(_saved_profile_cache) if _saved_profile_cache else None
    try:
        with open(_profile_path(), "r") as profile_file:
            payload = json.loads(profile_file.read())
        if payload.get("version") != CAMERA_PROFILE_VERSION:
            raise ValueError("配置版本不匹配")
        settings = _normalize_camera_settings(payload.get("settings", {}))
        if not settings:
            raise ValueError("参数为空")
        _saved_profile_cache = settings
        log("[摄像头] 已加载正式图像参数: {}".format(settings))
        return dict(settings)
    except OSError:
        _saved_profile_cache = None
        return None
    except Exception as e:
        log("[摄像头] 忽略无效图像参数文件: {}".format(e))
        _saved_profile_cache = None
        return None


def camera_profile_exists():
    return load_camera_profile() is not None


def _camera_get_setting(cam, name, fallback=None):
    getter = getattr(cam, "get_{}".format(name), None)
    if getter is not None:
        try:
            return getter()
        except Exception:
            pass
    try:
        return getattr(cam, name)
    except Exception:
        return fallback


def _camera_set_setting(cam, name, value):
    setter = getattr(cam, "set_{}".format(name), None)
    if setter is not None:
        setter(1 if isinstance(value, bool) and value else (0 if isinstance(value, bool) else value))
        return
    setattr(cam, name, value)


def _apply_settings_to_sensor(cam, settings, strict=False, only_names=None):
    """按依赖顺序写入传感器，返回失败说明列表。"""
    failures = []

    def apply_one(name):
        if name not in settings:
            return
        if only_names is not None and name not in only_names:
            return
        try:
            _camera_set_setting(cam, name, settings[name])
        except Exception as e:
            failures.append("{}: {}".format(name, e))

    for name in ("quality", "brightness", "contrast", "saturation",
                 "sharpness", "denoise", "ae_level", "aec2"):
        apply_one(name)

    # 白平衡、曝光和增益必须先切换自动控制，再写入相应的手动值。
    for name in ("whitebal", "awb_gain", "wb_mode", "exposure_ctrl"):
        apply_one(name)
    if not settings.get("exposure_ctrl", True):
        apply_one("aec_value")
    apply_one("gain_ctrl")
    if not settings.get("gain_ctrl", True):
        apply_one("agc_gain")

    if strict and failures:
        raise RuntimeError("; ".join(failures))
    return failures


def _startup_camera_settings():
    settings = safe_camera_settings()
    # 未明确保存前保留当前正式配置值，用于和实机稳定上限进行 A/B 对比。
    settings["quality"] = int(getattr(config, "CAM_JPEG_QUALITY", 10))
    saved = load_camera_profile()
    if saved:
        settings.update(saved)
    return settings


def get_camera_settings():
    """读取当前传感器实际值；不可读字段回退到最近成功应用的值。"""
    fallback = safe_camera_settings()
    fallback.update(_active_settings)
    if _cam_obj is None:
        return fallback
    current = {}
    for name, spec in _SETTING_SPECS.items():
        value = _camera_get_setting(_cam_obj, name, fallback.get(name))
        if spec[0] == "bool":
            value = bool(value)
        else:
            try:
                value = int(value)
            except Exception:
                value = fallback.get(name)
        current[name] = value
    return current


def apply_camera_settings(values, settle=True):
    """验证并增量应用网页参数；不会写入 Flash。"""
    global _active_settings
    if not _cam_ready or _cam_type != "cnadler86" or _cam_obj is None:
        raise RuntimeError("当前摄像头驱动不支持运行时调参")
    requested = _normalize_camera_settings(values)
    current = get_camera_settings()
    merged = dict(current)
    merged.update(requested)
    normalized = _normalize_camera_settings(merged)
    changed = set(
        name for name in requested
        if current.get(name) != normalized.get(name)
    )

    # 从自动切到手动时，即使数值看起来没有变化，也必须在关闭控制器之后
    # 重新写一次手动目标值；其余未变化寄存器（尤其 quality）不得重复写。
    if "exposure_ctrl" in changed and not normalized["exposure_ctrl"]:
        changed.add("aec_value")
    if "gain_ctrl" in changed and not normalized["gain_ctrl"]:
        changed.add("agc_gain")
    if "whitebal" in changed and not normalized["whitebal"]:
        changed.add("wb_mode")

    if changed:
        _apply_settings_to_sensor(
            _cam_obj, normalized, strict=True, only_names=changed)
    _active_settings = normalized
    if settle and changed:
        _sleep_ms(250)
        _discard_warmup_frames(1)
    return get_camera_settings()


def save_camera_profile(values):
    """验证并原子保存正式图像参数，返回写入后的完整配置。"""
    global _saved_profile_cache
    requested = _normalize_camera_settings(values)
    settings = safe_camera_settings()
    settings.update(get_camera_settings())
    settings.update(requested)
    settings = _normalize_camera_settings(settings)
    payload = {"version": CAMERA_PROFILE_VERSION, "settings": settings}
    path = _profile_path()
    from ondeskbot.storage import save_json_atomic
    save_json_atomic(path, payload)
    _saved_profile_cache = settings
    log("[摄像头] 正式图像参数已保存: {}".format(settings))
    return dict(settings)


def is_ready():
    return bool(_cam_ready)


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(now, before):
    return time.ticks_diff(now, before) if hasattr(time, "ticks_diff") else now - before


def _sleep_ms(ms):
    cooperative.sleep_ms(ms)


_JPEG_MARKERS = {
    0x01, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF,
    0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,
    0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF,
    0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7,
    0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF,
    0xFE,
}


def _u8(v):
    """MicroPython 的 memoryview 可能返回有符号 int，这里统一转成 0..255。"""
    return v & 0xFF


def _jpeg_bounds(buf):
    """返回 JPEG 有效数据的 (start, end)，end 为切片右边界。"""
    if not buf or len(buf) < 128:
        return None
    try:
        n = len(buf)
        start = -1
        # 少量驱动会在 DMA buffer 前面留下填充字节，允许 SOI 出现在开头附近。
        for i in range(0, min(64, n - 1)):
            if _u8(buf[i]) == 0xFF and _u8(buf[i + 1]) == 0xD8:
                start = i
                break
        if start < 0:
            return None

        end = -1
        # 有些固件返回的 memoryview 尾部带 padding，不能只看最后两个字节。
        for i in range(n - 2, start + 1, -1):
            if _u8(buf[i]) == 0xFF and _u8(buf[i + 1]) == 0xD9:
                end = i + 2
                break
        if end <= start + 128:
            return None
        return start, end
    except Exception:
        return None


def _is_jpeg(buf):
    """快速校验 JPEG SOI/EOI，避免把坏帧上传到云端。"""
    return _jpeg_bounds(buf) is not None


def _normalize_jpeg(buf):
    """返回裁掉前后填充后的 JPEG buffer；非法时返回 None。"""
    bounds = _jpeg_bounds(buf)
    if bounds is not None:
        start, end = bounds
        if start == 0 and end == len(buf):
            return buf
        try:
            return buf[start:end]
        except Exception:
            return bytes(buf[start:end])

    repaired = _repair_shifted_jpeg_markers(buf)
    if repaired and _jpeg_bounds(repaired):
        log("[摄像头] 检测到 JPEG 标记偏移(+0x0C), 已修复为标准 JPEG")
        return repaired
    return None


def _repair_shifted_jpeg_markers(buf):
    """
    修复某些 OV5640/cnadler86 组合返回的 marker 偏移帧。

    现场日志显示正常 JPEG 头 FF D8 FF E0 ... 被读成
    FF E4 FF EC ...，尾部 FF D9 被读成 FF E5。这个修复只在
    该特征出现时启用，并且只调整 0xFF 后面的 marker code。
    """
    if not buf or len(buf) < 128:
        return None
    try:
        n = len(buf)
        start = -1
        for i in range(0, min(64, n - 1)):
            if _u8(buf[i]) == 0xFF and _u8(buf[i + 1]) == 0xE4:
                start = i
                break
        if start < 0:
            return None

        end = -1
        for i in range(n - 2, start + 1, -1):
            if _u8(buf[i]) == 0xFF and _u8(buf[i + 1]) == 0xE5:
                end = i + 2
                break
        if end <= start + 128:
            return None

        out = bytearray(end - start)
        oi = 0
        i = start
        while i < end:
            b = _u8(buf[i])
            out[oi] = b
            oi += 1
            if b == 0xFF and i + 1 < end:
                nxt = _u8(buf[i + 1])
                orig = nxt - 0x0C
                if nxt not in (0x00, 0xFF) and orig in _JPEG_MARKERS:
                    nxt = orig
                out[oi] = nxt
                oi += 1
                i += 2
            else:
                i += 1
        if oi < len(out):
            out = out[:oi]
        return bytes(out)
    except Exception as e:
        log("[摄像头] JPEG 标记修复失败:", e)
        return None


def _frame_preview(buf):
    """用于日志的首尾字节预览。"""
    if not buf:
        return "empty"
    try:
        n = len(buf)
        head = " ".join("{:02X}".format(_u8(buf[i])) for i in range(min(8, n)))
        tail_start = max(0, n - 8)
        tail = " ".join("{:02X}".format(_u8(buf[i])) for i in range(tail_start, n))
        return "len={} head=[{}] tail=[{}]".format(n, head, tail)
    except Exception as e:
        return "preview_failed: {}".format(e)


def _camera_error_text(err):
    """把底层摄像头驱动常见英文异常转成中文日志。"""
    text = str(err)
    if "Failed to capture initial frame" in text:
        return "未能捕获初始化首帧，请使用合适的摄像头配置重新创建对象"
    return text


def _remember_frame_size():
    global _jpeg_w, _jpeg_h
    try:
        if _cam_obj and hasattr(_cam_obj, "get_pixel_width"):
            _jpeg_w = _cam_obj.get_pixel_width()
            _jpeg_h = _cam_obj.get_pixel_height()
            return
    except Exception:
        pass
    _jpeg_w = getattr(config, "CAM_IMAGE_WIDTH", 0)
    _jpeg_h = getattr(config, "CAM_IMAGE_HEIGHT", 0)


def get_frame_size():
    """返回当前 JPEG 图像尺寸，用于 ROI 元数据和坐标换算。"""
    if _jpeg_w > 0 and _jpeg_h > 0:
        return _jpeg_w, _jpeg_h
    return getattr(config, "CAM_IMAGE_WIDTH", 0), getattr(config, "CAM_IMAGE_HEIGHT", 0)


def _rgb565_to_bmp(data, width, height):
    """
    将 OV5640 输出的大端 RGB565 转换为 BMP 文件。
    步骤: 交换每像素的 2 字节 → 生成 16-bit BI_BITFIELDS BMP (bottom-up)。
    """
    ba = bytearray(data)
    n = len(ba)
    for i in range(0, n, 2):
        ba[i], ba[i + 1] = ba[i + 1], ba[i]

    row_bytes = width * 2
    pad = (4 - row_bytes % 4) % 4
    padded_row = row_bytes + pad
    pixel_size = padded_row * height
    header_size = 14 + 40 + 12
    file_size = header_size + pixel_size

    bmp = bytearray(header_size)
    struct.pack_into('<2sIHHI', bmp, 0,
                     b'BM', file_size, 0, 0, header_size)
    struct.pack_into('<IIIHHIIIIII', bmp, 14,
                     40, width, height, 1, 16,
                     3, pixel_size, 0, 0, 0, 0)
    struct.pack_into('<III', bmp, 54,
                     0xF800, 0x07E0, 0x001F)

    parts = [bytes(bmp)]
    for y in range(height - 1, -1, -1):
        off = y * row_bytes
        parts.append(ba[off:off + row_bytes])
        if pad:
            parts.append(b'\x00' * pad)

    return b''.join(parts)


def _camera_new(pixel_format=None, frame_size=None, jpeg_quality=None, fb_count=1):
    """创建 Camera 实例，显式传入 config.py 中定义的引脚。"""
    from camera import Camera, FrameSize, PixelFormat
    if pixel_format is None:
        pixel_format = PixelFormat.RGB565
    if frame_size is None:
        frame_size = FrameSize.QVGA
    if jpeg_quality is None:
        jpeg_quality = _startup_camera_settings().get(
            "quality", config.CAM_JPEG_QUALITY)
    return Camera(
        data_pins=[
            config.CAM_PIN_D0, config.CAM_PIN_D1,
            config.CAM_PIN_D2, config.CAM_PIN_D3,
            config.CAM_PIN_D4, config.CAM_PIN_D5,
            config.CAM_PIN_D6, config.CAM_PIN_D7,
        ],
        vsync_pin=config.CAM_PIN_VSYNC,
        href_pin=config.CAM_PIN_HREF,
        sda_pin=config.CAM_PIN_SIOD,
        scl_pin=config.CAM_PIN_SIOC,
        pclk_pin=config.CAM_PIN_PCLK,
        xclk_pin=config.CAM_PIN_XCLK,
        xclk_freq=config.CAM_XCLK_FREQ,
        powerdown_pin=config.CAM_PIN_PWDN,
        reset_pin=config.CAM_PIN_RESET,
        pixel_format=pixel_format,
        frame_size=frame_size,
        jpeg_quality=jpeg_quality,
        fb_count=fb_count,
    )


def _set_sensor_bool(cam, method_names, value):
    """调用不同 camera 固件提供的第一个可用布尔设置方法。"""
    for name in method_names:
        method = getattr(cam, name, None)
        if method is None:
            continue
        try:
            method(1 if value else 0)
            return name
        except Exception as e:
            log("[摄像头] 方向设置 {} 失败: {}".format(name, e))
            return None
    return None


def _apply_sensor_orientation(cam):
    """
    在传感器层统一校正方向。

    标定页面和模型上传都直接使用 capture() 返回的 JPEG，因此镜像只能在
    这里处理一次；禁止页面 CSS 或坐标换算再做第二次镜像。
    """
    vflip = bool(getattr(config, "CAM_VFLIP", False))
    hmirror = bool(getattr(config, "CAM_HMIRROR", False))
    v_method = _set_sensor_bool(cam, ("set_vflip", "vflip", "flip"), vflip)
    h_method = _set_sensor_bool(cam, ("set_hmirror", "hmirror", "mirror"), hmirror)

    # False 可由传感器默认状态满足；True 则必须找到并成功调用驱动方法。
    if vflip and v_method is None:
        log("[摄像头] 错误: 当前驱动不支持所需的垂直翻转")
        return False
    if hmirror and h_method is None:
        log("[摄像头] 错误: 当前驱动不支持所需的水平去镜像")
        return False

    log("[摄像头] JPEG 方向: 垂直翻转={}({}), 水平去镜像={}({})".format(
        vflip, v_method or "默认", hmirror, h_method or "默认"))
    return True


def _tune_sensor(cam):
    """校正方向并调优传感器参数 (OV3660 / OV5640 均适用)。"""
    global _active_settings
    if not _apply_sensor_orientation(cam):
        raise RuntimeError("无法应用摄像头方向配置")
    settings = _startup_camera_settings()
    failures = _apply_settings_to_sensor(cam, settings)
    for name in ("lenc", "raw_gma", "bpc", "wpc"):
        try:
            _camera_set_setting(cam, name, True)
        except Exception as e:
            failures.append("{}: {}".format(name, e))
    _active_settings = settings
    if failures:
        log("[摄像头] 传感器调优部分失败(不影响拍照): {}".format(
            "; ".join(failures)))
    else:
        log("[摄像头] 传感器图像参数已应用: quality={} sharpness={}".format(
            settings.get("quality"), settings.get("sharpness")))


def _restore_sensor_profile(cam, apply_quality=False):
    """
    重新应用运行期传感器配置。

    某些 camera 固件在 重配置() 后会丢失 vflip/hmirror 等方向设置，
    导致 JPEG 标定图与 RGB565 裁剪图方向不一致。这里统一在 重配置()
    成功后重新写回方向参数；JPEG 模式下顺带恢复质量参数。
    """
    if cam is None:
        return
    _tune_sensor(cam)
    # quality 已包含在正式 profile 中；保留 apply_quality 参数只是兼容旧调用点。


def _discard_warmup_frames(count=None):
    """初始化或重启后丢弃若干帧，等待曝光/白平衡稳定。"""
    if count is None:
        count = getattr(config, "CAM_WARMUP_FRAMES", 3)
    if count <= 0 or not _cam_ready:
        return
    for _ in range(count):
        try:
            if _cam_type == "cnadler86" and _cam_obj:
                dummy = _cam_obj.capture()
            elif _cam_type == "kaki5":
                import camera as _cam
                dummy = _cam.capture_jpg() if hasattr(_cam, "capture_jpg") else _cam.capture()
            elif _cam_type == "esp32_camera" and _cam_obj:
                dummy = _cam_obj.capture()
            else:
                dummy = None
            if dummy:
                del dummy
        except Exception as e:
            log("[摄像头] 预热丢帧失败:", e)
        gc.collect()
        _sleep_ms(150)


def _capture_valid_jpeg(max_tries=3, label="JPEG"):
    """捕获并校验 JPEG，成功返回规范化后的 buffer。"""
    for _try in range(max_tries):
        gc.collect()
        buf = _cam_obj.capture()
        jpeg = _normalize_jpeg(buf)
        if jpeg:
            if jpeg is not buf:
                log("[摄像头] {} 捕获含填充, 已裁剪: {} -> {} 字节".format(
                    label, len(buf), len(jpeg)))
                try:
                    del buf
                except Exception:
                    pass
            return jpeg
        log("[摄像头] {} 非法帧: {}".format(label, _frame_preview(buf)))
        try:
            del buf
        except Exception:
            pass
        _sleep_ms(500)
    return None


def begin():
    """
    初始化摄像头，自动适配不同固件 API。
    成功返回 True。
    """
    global _cam_type, _cam_ready, _cam_obj, _jpeg_broken
    global _capture_count_since_restart, _last_capture_ms, _jpeg_w, _jpeg_h
    global _saved_profile_cache, _active_settings

    _capture_count_since_restart = 0
    _last_capture_ms = 0
    _jpeg_w = 0
    _jpeg_h = 0
    _saved_profile_cache = False
    _active_settings = {}

    # ── 方式 1: cnadler86/micropython-camera-API ──
    try:
        from camera import Camera, FrameSize, PixelFormat  # noqa: F401
        log("[摄像头] cnadler86 Camera 模块已导入")

        if _cam_obj is not None:
            try:
                _cam_obj.deinit()
            except Exception:
                pass
            _cam_obj = None
            cooperative.sleep_ms(500)

        _cam_obj = _camera_new()
        sensor = _cam_obj.get_sensor_name() if hasattr(_cam_obj, 'get_sensor_name') else 'OV5640'
        pf = _cam_obj.get_pixel_format() if hasattr(_cam_obj, 'get_pixel_format') else '?'
        fs = _cam_obj.get_frame_size() if hasattr(_cam_obj, 'get_frame_size') else '?'
        log("[摄像头] 传感器: {}, 默认像素格式={} 帧尺寸={}".format(sensor, pf, fs))

        jpeg_ok = False
        best_fs_name = "VGA"
        best_fs = FrameSize.VGA

        if "OV5640" in sensor:
            log("[摄像头] OV5640: 优先尝试直接 JPEG 初始化")
            try:
                _cam_obj.deinit()
            except Exception:
                pass
            cooperative.sleep_ms(800)
            _cam_obj = None
            gc.collect()
            for target_name, target_fs in [
                ("QVGA", FrameSize.QVGA),
                ("VGA",  FrameSize.VGA),
                ("SVGA", FrameSize.SVGA),
            ]:
                try:
                    _cam_obj = _camera_new(
                        pixel_format=PixelFormat.JPEG,
                        frame_size=target_fs,
                        jpeg_quality=_startup_camera_settings().get(
                            "quality", config.CAM_JPEG_QUALITY),
                        fb_count=1,
                    )
                    cooperative.sleep_ms(700)
                    _tune_sensor(_cam_obj)
                    cooperative.sleep_ms(300)
                    _test = _capture_valid_jpeg(3, "直接JPEG {}".format(target_name))
                    if _test:
                        jpeg_ok = True
                        best_fs_name = target_name
                        best_fs = target_fs
                        log("[摄像头] 直接 JPEG 验证通过 ({}): {} 字节".format(
                            target_name, len(_test)))
                        del _test
                        break
                except Exception as e:
                    log("[摄像头] 直接 JPEG {} 初始化失败: {}".format(
                        target_name, _camera_error_text(e)))
                try:
                    if _cam_obj:
                        _cam_obj.deinit()
                except Exception:
                    pass
                _cam_obj = None
                cooperative.sleep_ms(500)
                gc.collect()

        if not jpeg_ok:
            if _cam_obj is None:
                _cam_obj = _camera_new()
                cooperative.sleep_ms(500)

            for target_name, target_fs in [
                ("QVGA", FrameSize.QVGA),
                ("VGA",  FrameSize.VGA),
            ]:
                try:
                    _cam_obj.reconfigure(
                        pixel_format=PixelFormat.JPEG,
                        frame_size=target_fs,
                    )
                    cooperative.sleep_ms(700)
                    _tune_sensor(_cam_obj)
                    cooperative.sleep_ms(300)
                    _test = _capture_valid_jpeg(3, "重配置 JPEG {}".format(target_name))
                    if _test:
                        jpeg_ok = True
                        best_fs_name = target_name
                        best_fs = target_fs
                        log("[摄像头] {} JPEG 验证通过: {} 字节".format(
                            target_name, len(_test)))
                        del _test
                        break
                except Exception as e:
                    log("[摄像头] 重配置 JPEG {} 失败: {}".format(
                        target_name, _camera_error_text(e)))
                cooperative.sleep_ms(500)

        if not jpeg_ok:
            log("[摄像头] JPEG 失败, 重初始化再试...")
            try:
                _cam_obj.deinit()
            except Exception:
                pass
            cooperative.sleep_ms(1000)
            _cam_obj = _camera_new()
            cooperative.sleep_ms(500)
            for _ in range(5):
                _cam_obj.capture()
                cooperative.sleep_ms(100)
            _cam_obj.reconfigure(
                pixel_format=PixelFormat.JPEG,
                frame_size=FrameSize.VGA,
            )
            cooperative.sleep_ms(1000)
            _tune_sensor(_cam_obj)
            cooperative.sleep_ms(500)
            _test = _capture_valid_jpeg(3, "重试 JPEG VGA")
            if _test:
                jpeg_ok = True
                best_fs_name = "VGA"
                best_fs = FrameSize.VGA
                log("[摄像头] 重试 JPEG 通过: {} 字节".format(len(_test)))
                del _test

        if jpeg_ok:
            upgrade_targets = [
                ("QVGA", FrameSize.QVGA),
                ("VGA",  FrameSize.VGA),
                ("SVGA", FrameSize.SVGA),
                ("XGA",  FrameSize.XGA),
                ("SXGA", FrameSize.SXGA),
                ("UXGA", FrameSize.UXGA),
            ]
            start_upgrade = False
            for target_name, target_fs in upgrade_targets:
                if target_fs == best_fs:
                    start_upgrade = True
                    continue
                if not start_upgrade:
                    continue
                settle_ms = getattr(config, "CAM_UPGRADE_SETTLE_MS", 900)
                validate_tries = getattr(config, "CAM_UPGRADE_VALIDATE_TRIES", 3)
                _cam_obj.set_frame_size(target_fs)
                cooperative.sleep_ms(settle_ms)
                _restore_sensor_profile(_cam_obj, apply_quality=True)
                gc.collect()
                _test = _capture_valid_jpeg(validate_tries, "升级JPEG {}".format(target_name))
                upgrade_method = "set_frame_size"
                if not _test:
                    log("[摄像头] 升级 JPEG {} 使用 set_frame_size 失败，尝试重配置".format(
                        target_name))
                    try:
                        _cam_obj.reconfigure(
                            pixel_format=PixelFormat.JPEG,
                            frame_size=target_fs,
                        )
                        cooperative.sleep_ms(settle_ms)
                        _restore_sensor_profile(_cam_obj, apply_quality=True)
                        gc.collect()
                        _test = _capture_valid_jpeg(
                            validate_tries, "重配置升级 JPEG {}".format(target_name))
                        upgrade_method = "reconfigure"
                    except Exception as e:
                        log("[摄像头] 升级 JPEG {} 重配置失败: {}".format(
                            target_name, _camera_error_text(e)))
                if _test:
                    log("[摄像头] 升级 JPEG {} 验证通过: {} 字节 ({})".format(
                        target_name, len(_test), upgrade_method))
                    best_fs_name = target_name
                    best_fs = target_fs
                    del _test
                    cooperative.sleep_ms(200)
                else:
                    log("[摄像头] 升级 JPEG {} 失败，回退到 {}".format(
                        target_name, best_fs_name))
                    try:
                        _cam_obj.reconfigure(
                            pixel_format=PixelFormat.JPEG,
                            frame_size=best_fs,
                        )
                    except Exception:
                        _cam_obj.set_frame_size(best_fs)
                    _restore_sensor_profile(_cam_obj, apply_quality=True)
                    cooperative.sleep_ms(300)
                    break

            _jpeg_broken = False
            _cam_type = "cnadler86"
            _cam_ready = True
            _remember_frame_size()
            log("[摄像头] 初始化成功 (JPEG {}), {}x{}".format(
                best_fs_name, _jpeg_w, _jpeg_h))
            _discard_warmup_frames()
            return True

        log("[摄像头] JPEG 编码器不可用或输出坏帧")
        if getattr(config, "CAM_FORCE_JPEG", True):
            log("[摄像头] CAM_FORCE_JPEG=True, 禁止 RGB565/BMP 降级")
            _jpeg_broken = True
            _cam_ready = False
            try:
                _cam_obj.deinit()
            except Exception:
                pass
            _cam_obj = None
            return False

        log("[摄像头] CAM_FORCE_JPEG=False, 降级到 RGB565+BMP 模式")
        _cam_obj.deinit()
        cooperative.sleep_ms(500)
        _cam_obj = _camera_new()
        cooperative.sleep_ms(300)
        _cam_obj.reconfigure(
            pixel_format=PixelFormat.RGB565,
            frame_size=FrameSize.VGA,
        )
        cooperative.sleep_ms(500)
        _tune_sensor(_cam_obj)

        gc.collect()
        _test = _cam_obj.capture()
        if _test and len(_test) == 640 * 480 * 2:
            _jpeg_broken = True
            _cam_type = "cnadler86"
            _cam_ready = True
            log("[摄像头] 初始化成功 (RGB565 VGA 降级模式), {} 字节".format(len(_test)))
            del _test
            gc.collect()
            return True

        log("[摄像头] RGB565 VGA 也失败 (内存={})".format(gc.mem_free()))
        _cam_ready = False
        return False
    except ImportError:
        log("[摄像头] 未找到 cnadler86 Camera 模块")
    except Exception as e:
        log("[摄像头] cnadler86 初始化失败: {}".format(_camera_error_text(e)))

    # ── 方式 2: kaki5 Freenove 固件 ──
    try:
        import camera as _cam
        log("[摄像头] kaki5 摄像头模块已导入")

        try:
            _cam.deinit()
        except Exception:
            pass
        cooperative.sleep_ms(500)

        _cam.init()
        if hasattr(_cam, 'pixformat'):
            _cam.pixformat(0)
        _cam.framesize(config.CAM_FRAMESIZE)
        _cam.quality(config.CAM_JPEG_QUALITY)
        if not _apply_sensor_orientation(_cam):
            raise RuntimeError("kaki5 驱动无法应用摄像头方向配置")
        _cam_type = "kaki5"
        _cam_ready = True

        _test = _cam.capture_jpg() if hasattr(_cam, 'capture_jpg') else _cam.capture()
        ok = _is_jpeg(_test) and not isinstance(_test, bool)
        log("[摄像头] 初始化成功 (kaki5), 测试拍照: {}".format(
            "{} 字节".format(len(_test)) if ok else "失败"))
        if ok:
            _jpeg_broken = False
            _remember_frame_size()
            del _test
            gc.collect()
            _discard_warmup_frames()
            return True
        _cam_ready = False
        _jpeg_broken = True
        del _test
        gc.collect()
        if getattr(config, "CAM_FORCE_JPEG", True):
            log("[摄像头] kaki5 未输出合法 JPEG, 初始化失败")
            return False
        return True
    except ImportError:
        log("[摄像头] 未找到 kaki5 camera 模块")
    except Exception as e:
        log("[摄像头] kaki5 初始化失败: {}".format(_camera_error_text(e)))

    # ── 方式 3: esp32_camera 模块 ──
    try:
        from esp32_camera import Camera as EspCam
        _cam_obj = EspCam(
            d0=config.CAM_PIN_D0, d1=config.CAM_PIN_D1,
            d2=config.CAM_PIN_D2, d3=config.CAM_PIN_D3,
            d4=config.CAM_PIN_D4, d5=config.CAM_PIN_D5,
            d6=config.CAM_PIN_D6, d7=config.CAM_PIN_D7,
            xclk=config.CAM_PIN_XCLK, pclk=config.CAM_PIN_PCLK,
            vsync=config.CAM_PIN_VSYNC, href=config.CAM_PIN_HREF,
            siod=config.CAM_PIN_SIOD, sioc=config.CAM_PIN_SIOC,
            reset=config.CAM_PIN_RESET, pwdn=config.CAM_PIN_PWDN,
            xclk_freq=config.CAM_XCLK_FREQ,
            jpeg_quality=config.CAM_JPEG_QUALITY,
            fb_location=1,
            frame_size=config.CAM_FRAMESIZE,
            pixel_format=0,
        )
        _cam_obj.init()
        if not _apply_sensor_orientation(_cam_obj):
            raise RuntimeError("esp32_camera 驱动无法应用摄像头方向配置")
        _cam_type = "esp32_camera"
        _cam_ready = True
        _test = _cam_obj.capture()
        if _is_jpeg(_test):
            _jpeg_broken = False
            _remember_frame_size()
            log("[摄像头] 初始化成功 (esp32_camera), 测试 JPEG: {} 字节".format(len(_test)))
            del _test
            gc.collect()
            _discard_warmup_frames()
            return True
        if _test:
            del _test
        _cam_ready = False
        _jpeg_broken = True
        log("[摄像头] esp32_camera 未输出合法 JPEG")
        return False
    except ImportError:
        log("[摄像头] 未找到 esp32_camera 模块")
    except Exception as e:
        log("[摄像头] esp32_camera 初始化失败: {}".format(_camera_error_text(e)))

    log("[摄像头] 错误: 所有摄像头初始化方式均失败")
    return False


def _rate_limit_capture():
    """限制连续拍照频率，降低传感器/PSRAM/堆压力。"""
    global _last_capture_ms
    min_interval = getattr(config, "CAM_CAPTURE_MIN_INTERVAL_MS", 0)
    if min_interval <= 0 or _last_capture_ms <= 0:
        return
    now = _ticks_ms()
    elapsed = _ticks_diff(now, _last_capture_ms)
    if elapsed < min_interval:
        _sleep_ms(min_interval - elapsed)


def _restart_camera_for_stability():
    """长期运行时定期重启 摄像头驱动，释放可能积累的状态漂移。"""
    global _restart_guard
    if _restart_guard:
        return False
    _restart_guard = True
    try:
        log("[摄像头] 达到周期重启阈值, 重启摄像头驱动...")
        deinit()
        _sleep_ms(800)
        ok = begin()
        if ok:
            log("[摄像头] 周期重启完成")
        else:
            log("[摄像头] 周期重启失败")
        return ok
    finally:
        _restart_guard = False


def _periodic_restart_if_needed():
    limit = getattr(config, "CAM_RESTART_EVERY_N_CAPTURES", 0)
    if limit <= 0:
        return True
    if _capture_count_since_restart < limit:
        return True
    return _restart_camera_for_stability()


def _note_capture_success():
    """记录拍照成功，清除连续失败计数。"""
    global _consecutive_capture_failures
    _consecutive_capture_failures = 0


def _note_capture_failure(reason=""):
    """记录拍照失败，连续失败后自动重启摄像头驱动。"""
    global _consecutive_capture_failures
    _consecutive_capture_failures += 1
    limit = getattr(config, "CAM_RECOVER_AFTER_N_CAPTURE_FAILURES", 0)
    if limit > 0 and _consecutive_capture_failures >= limit:
        suffix = "，原因: {}".format(reason) if reason else ""
        log("[摄像头] 连续拍照失败 {} 次{}，重启摄像头驱动...".format(
            _consecutive_capture_failures, suffix))
        _consecutive_capture_failures = 0
        _restart_camera_for_stability()


def capture():
    """
    拍摄一张 JPEG 照片，返回 字节。
    失败返回 None。
    """
    global _capture_count_since_restart, _last_capture_ms

    if not _cam_ready:
        log("[摄像头] 未初始化")
        return None

    if not _periodic_restart_if_needed():
        return None

    _rate_limit_capture()
    gc.collect()

    try:
        if _cam_type == "cnadler86":
            buf = _cam_obj.capture()
            if not buf:
                log("[摄像头] 拍照失败 (内存={})".format(gc.mem_free()))
                _note_capture_failure("空帧")
                return None
            if _jpeg_broken:
                log("[摄像头] JPEG 不可用, CAM_FORCE_JPEG 模式下拒绝 BMP/RGB565 输出")
                _note_capture_failure("JPEG不可用")
                return None
            jpeg = _normalize_jpeg(buf)
            if jpeg:
                _note_capture_success()
                _capture_count_since_restart += 1
                _last_capture_ms = _ticks_ms()
                _remember_frame_size()
                if jpeg is not buf:
                    log("[摄像头] 拍照成功: {} 字节 (由 {} 字节裁剪)".format(
                        len(jpeg), len(buf)))
                    try:
                        del buf
                    except Exception:
                        pass
                    return jpeg
                log("[摄像头] 拍照成功: {} 字节".format(len(buf)))
                return jpeg
            log("[摄像头] 拍照失败: 非法 JPEG 帧 ({}, 内存={})".format(
                _frame_preview(buf), gc.mem_free()))
            _note_capture_failure("非法JPEG")
            return None

        elif _cam_type == "kaki5":
            import camera as _cam
            buf = None
            if hasattr(_cam, 'capture_jpg'):
                buf = _cam.capture_jpg()
            if not buf or isinstance(buf, bool):
                buf = None
                if hasattr(_cam, 'capture'):
                    buf = _cam.capture()
                if isinstance(buf, bool):
                    buf = None
            jpeg = _normalize_jpeg(buf)
            if jpeg:
                _note_capture_success()
                _capture_count_since_restart += 1
                _last_capture_ms = _ticks_ms()
                if jpeg is not buf:
                    try:
                        del buf
                    except Exception:
                        pass
                log("[摄像头] 拍照成功: {} 字节".format(len(jpeg)))
                return jpeg
            log("[摄像头] 拍照失败: 非法 JPEG 帧 ({}, 内存={})".format(
                _frame_preview(buf), gc.mem_free()))
            _note_capture_failure("非法JPEG")
            return None

        elif _cam_type == "esp32_camera":
            buf = _cam_obj.capture()
            jpeg = _normalize_jpeg(buf)
            if jpeg:
                _note_capture_success()
                _capture_count_since_restart += 1
                _last_capture_ms = _ticks_ms()
                _remember_frame_size()
                if jpeg is not buf:
                    try:
                        del buf
                    except Exception:
                        pass
                log("[摄像头] 拍照成功: {} 字节".format(len(jpeg)))
                return jpeg
            log("[摄像头] 拍照失败: 非法 JPEG 帧 ({})".format(_frame_preview(buf)))
            _note_capture_failure("非法JPEG")
            return None

    except Exception as e:
        log("[摄像头] 拍照失败:", e)
        _note_capture_failure(str(e))

    return None


def capture_base64():
    """
    拍照并返回 base64 编码字符串 (不含 data URI 前缀)。
    失败返回 None。
    """
    buf = capture()
    if buf is None:
        return None

    gc.collect()

    try:
        b64_bytes = ubinascii.b2a_base64(buf)
        b64_str = b64_bytes.decode("ascii").replace("\n", "").replace("\r", "")
        del buf
        del b64_bytes
        gc.collect()
        log("[摄像头] base64 编码长度: {}".format(len(b64_str)))
        return b64_str
    except Exception as e:
        log("[摄像头] base64 编码失败:", e)
        return None


def capture_jpeg_data_uri_parts():
    """
    拍照并返回 JPEG data URI 分块列表。

    返回格式:
      ["data:image/jpeg;base64,", "...", "..."]

    调用方可分段发送，避免在内存中构造一个巨大的 base64 字符串。
    """
    buf = capture()
    if buf is None:
        return None
    if not _is_jpeg(buf):
        log("[摄像头] 拒绝上传: 非 JPEG 数据")
        del buf
        gc.collect()
        return None

    chunk_size = getattr(config, "CAM_BASE64_CHUNK_SIZE", 3072)
    if chunk_size < 768:
        chunk_size = 768
    chunk_size -= chunk_size % 3
    if chunk_size <= 0:
        chunk_size = 3072

    parts = ["data:image/jpeg;base64,"]
    total_b64 = 0
    try:
        mv = memoryview(buf)
        for off in range(0, len(buf), chunk_size):
            end = min(off + chunk_size, len(buf))
            try:
                b64_bytes = ubinascii.b2a_base64(mv[off:end])
            except TypeError:
                b64_bytes = ubinascii.b2a_base64(buf[off:end])
            b64_str = b64_bytes.decode("ascii").replace("\n", "").replace("\r", "")
            total_b64 += len(b64_str)
            parts.append(b64_str)
            del b64_bytes
        del mv
        del buf
        gc.collect()
        log("[摄像头] JPEG base64 分块: {} 段, {} 字符".format(
            len(parts) - 1, total_b64))
        return parts
    except Exception as e:
        log("[摄像头] JPEG 分块 base64 编码失败:", e)
        try:
            del buf
        except Exception:
            pass
        gc.collect()
        return None


def capture_base64_data_uri():
    """
    拍照并返回完整的 data URI。
    JPEG 模式: "data:image/jpeg;base64,xxxxx..."
    失败返回 None。
    """
    parts = capture_jpeg_data_uri_parts()
    if not parts:
        return None
    try:
        return "".join(parts)
    finally:
        del parts
        gc.collect()


_rgb565_mode = False
_rgb565_w = 0
_rgb565_h = 0


def _recover_jpeg():
    """重配置 失败后恢复 JPEG 模式，必要时完全重新初始化摄像头。"""
    global _cam_obj
    from camera import FrameSize, PixelFormat
    try:
        _cam_obj.reconfigure(
            pixel_format=PixelFormat.JPEG,
            frame_size=FrameSize.VGA,
        )
        cooperative.sleep_ms(300)
        _restore_sensor_profile(_cam_obj, apply_quality=True)
        log("[摄像头] 已恢复 JPEG VGA")
        return
    except Exception:
        pass
    log("[摄像头] 重配置恢复失败, 重新初始化摄像头...")
    try:
        _cam_obj.deinit()
    except Exception:
        pass
    cooperative.sleep_ms(500)
    gc.collect()
    try:
        _cam_obj = _camera_new()
        cooperative.sleep_ms(300)
        _cam_obj.reconfigure(
            pixel_format=PixelFormat.JPEG,
            frame_size=FrameSize.VGA,
        )
        cooperative.sleep_ms(300)
        _restore_sensor_profile(_cam_obj, apply_quality=True)
        log("[摄像头] 重新初始化成功 (JPEG VGA)")
    except Exception as e:
        log("[摄像头] 重新初始化失败: {}".format(_camera_error_text(e)))


def reconfigure_rgb565():
    """
    切换摄像头到 RGB565 模式，尝试最高可用分辨率。
    必须用 重配置() 而非 set_frame_size()，因为后者不会重新分配帧缓冲区。
    重配置() 失败会导致摄像头进入无效状态，需要 deinit+reinit 恢复。
    返回 (success, width, height)。
    """
    global _rgb565_mode, _rgb565_w, _rgb565_h, _cam_obj

    if getattr(config, "CAM_FORCE_JPEG", True):
        log("[摄像头] CAM_FORCE_JPEG=True, 禁止切换 RGB565")
        return False, 0, 0

    if _cam_type != "cnadler86" or not _cam_obj:
        log("[摄像头] RGB565 模式仅支持 cnadler86 驱动")
        return False, 0, 0

    from camera import FrameSize, PixelFormat

    targets = [
        ("UXGA", FrameSize.UXGA, 1600, 1200),
        ("SXGA", FrameSize.SXGA, 1280, 1024),
        ("XGA",  FrameSize.XGA,  1024,  768),
        ("SVGA", FrameSize.SVGA,  800,  600),
        ("VGA",  FrameSize.VGA,   640,  480),
    ]

    for name, fs, w, h in targets:
        gc.collect()
        log("[摄像头] 尝试 RGB565 {} ({}x{}, 需要 {}KB)...".format(
            name, w, h, w * h * 2 // 1024))
        try:
            _cam_obj.reconfigure(
                pixel_format=PixelFormat.RGB565,
                frame_size=fs,
                fb_count=1,
            )
        except Exception as e:
            log("[摄像头] RGB565 {} 重配置异常: {}".format(name, e))
            try:
                _cam_obj.deinit()
            except Exception:
                pass
            cooperative.sleep_ms(500)
            gc.collect()
            _cam_obj = _camera_new()
            cooperative.sleep_ms(300)
            continue
        cooperative.sleep_ms(500)
        _restore_sensor_profile(_cam_obj)
        gc.collect()
        test = _cam_obj.capture()
        expected = w * h * 2
        if test and len(test) == expected:
            del test
            gc.collect()
            _rgb565_mode = True
            _rgb565_w = w
            _rgb565_h = h
            log("[摄像头] RGB565 模式: {} ({}x{})".format(name, w, h))
            return True, w, h
        actual = len(test) if test else 0
        if test:
            del test
        log("[摄像头] RGB565 {} 捕获失败 (实际 {} 期望 {})".format(
            name, actual, expected))

    log("[摄像头] RGB565 所有分辨率均失败")
    _recover_jpeg()
    return False, 0, 0


def reconfigure_jpeg_uxga():
    """恢复 JPEG UXGA 模式。返回 True/False。"""
    global _rgb565_mode, _rgb565_w, _rgb565_h

    if _cam_type != "cnadler86" or not _cam_obj:
        return False

    from camera import FrameSize, PixelFormat

    _cam_obj.reconfigure(
        pixel_format=PixelFormat.JPEG,
        frame_size=FrameSize.VGA,
    )
    cooperative.sleep_ms(500)
    _restore_sensor_profile(_cam_obj, apply_quality=True)

    best_fs = FrameSize.VGA
    for target_fs in [FrameSize.XGA, FrameSize.SXGA, FrameSize.UXGA]:
        _cam_obj.set_frame_size(target_fs)
        cooperative.sleep_ms(500)
        gc.collect()
        test = _cam_obj.capture()
        if not test or len(test) <= 100:
            _cam_obj.set_frame_size(best_fs)
            cooperative.sleep_ms(300)
            break
        best_fs = target_fs
        del test
        cooperative.sleep_ms(200)

    _rgb565_mode = False
    _rgb565_w = 0
    _rgb565_h = 0
    _remember_frame_size()
    log("[摄像头] 已恢复 JPEG 模式: {}x{}".format(
        _jpeg_w, _jpeg_h))
    return True


def capture_raw_copy():
    """
    RGB565 模式下拍照，返回像素数据的独立拷贝。
    返回 (buf_copy, width, height) 或 (None, 0, 0)。
    """
    if getattr(config, "CAM_FORCE_JPEG", True):
        log("[摄像头] CAM_FORCE_JPEG=True, 禁止 RGB565 原始数据拷贝")
        return None, 0, 0
    if not _cam_ready or not _rgb565_mode:
        log("[摄像头] 未就绪或不在 RGB565 模式")
        return None, 0, 0

    gc.collect()
    try:
        raw = _cam_obj.capture()
        if not raw:
            log("[摄像头] RGB565 拍照失败 (内存={})".format(gc.mem_free()))
            return None, 0, 0

        expected = _rgb565_w * _rgb565_h * 2
        if len(raw) != expected:
            log("[摄像头] RGB565 尺寸异常: {} vs {}".format(len(raw), expected))
            return None, 0, 0

        buf_copy = bytearray(raw)
        del raw
        gc.collect()
        log("[摄像头] RGB565 拍照成功: {}x{}, {} 字节".format(
            _rgb565_w, _rgb565_h, len(buf_copy)))
        return buf_copy, _rgb565_w, _rgb565_h
    except Exception as e:
        log("[摄像头] RGB565 拍照异常:", e)
        return None, 0, 0


def capture_raw_ref():
    """
    RGB565 模式下拍照，返回 DMA 缓冲区的直接引用（零拷贝）。
    节省 2.5MB 内存，但必须在下次 capture() 前完成处理。
    返回 (buf_ref, width, height) 或 (None, 0, 0)。
    """
    if getattr(config, "CAM_FORCE_JPEG", True):
        log("[摄像头] CAM_FORCE_JPEG=True, 禁止 RGB565 原始数据引用")
        return None, 0, 0
    if not _cam_ready or not _rgb565_mode:
        log("[摄像头] 未就绪或不在 RGB565 模式")
        return None, 0, 0

    gc.collect()
    try:
        raw = _cam_obj.capture()
        if not raw:
            log("[摄像头] RGB565 拍照失败 (内存={})".format(gc.mem_free()))
            return None, 0, 0

        expected = _rgb565_w * _rgb565_h * 2
        if len(raw) != expected:
            log("[摄像头] RGB565 尺寸异常: {} vs {}".format(len(raw), expected))
            return None, 0, 0

        log("[摄像头] RGB565 拍照成功 (零拷贝): {}x{}, {} 字节, 内存={}".format(
            _rgb565_w, _rgb565_h, len(raw), gc.mem_free()))
        return raw, _rgb565_w, _rgb565_h
    except Exception as e:
        log("[摄像头] RGB565 拍照异常:", e)
        return None, 0, 0


def deinit():
    """释放摄像头资源"""
    global _cam_ready, _cam_obj, _rgb565_mode, _rgb565_w, _rgb565_h
    global _jpeg_w, _jpeg_h, _capture_count_since_restart, _last_capture_ms
    global _consecutive_capture_failures
    try:
        if _cam_type == "cnadler86" and _cam_obj:
            _cam_obj.deinit()
            _cam_obj = None
        elif _cam_type == "kaki5":
            import camera as _cam
            _cam.deinit()
        elif _cam_type == "esp32_camera" and _cam_obj:
            _cam_obj.deinit()
            _cam_obj = None
        _cam_ready = False
        _rgb565_mode = False
        _rgb565_w = 0
        _rgb565_h = 0
        _jpeg_w = 0
        _jpeg_h = 0
        _capture_count_since_restart = 0
        _last_capture_ms = 0
        _consecutive_capture_failures = 0
        log("[摄像头] 已释放")
    except Exception as e:
        log("[摄像头] 释放失败:", e)

