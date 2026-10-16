"""
ArduinoDriver UART 通信模块

完全兼容 ArduinoDriver/PROTOCOL.txt:
  - Move,x_steps,y_steps
  - MoveClick,x_ratio,y_ratio[,click_count,delay_ms]
  - Drag,start_x,start_y,end_x,end_y
  - ZClick[,click_count,delay_ms]
  - Home
  - GetStatus

协议规则:
  ESP32 -> Arduino: 一行文本指令, \\n 结尾
  Arduino -> ESP32: 单行 JSON 响应
"""

import machine
import json
import time
import config
from ondeskbot import cooperative
from ondeskbot.log import log


_uart = None
_framing_supported = None
_next_sequence = 0


def begin():
    """
    初始化 UART 连接到 ArduinoDriver。
    使用 config 中定义的引脚和波特率。
    """
    global _uart, _framing_supported, _next_sequence

    if _uart is not None:
        try:
            _uart.deinit()
        except Exception:
            pass
        _uart = None

    cooperative.sleep_ms(50)

    _uart = machine.UART(
        config.UART_ID,
        baudrate=config.UART_BAUD,
        tx=config.UART_TX_PIN,
        rx=config.UART_RX_PIN,
        rxbuf=512,
        timeout=10,
        timeout_char=10,
    )
    cooperative.sleep_ms(100)
    _flush_input()
    _framing_supported = None
    _next_sequence = time.ticks_ms() & 0xFFFF
    log("[串口] 初始化完成: UART{} 发送引脚=GPIO{} 接收引脚=GPIO{} 波特率={}".format(
        config.UART_ID, config.UART_TX_PIN, config.UART_RX_PIN, config.UART_BAUD))


def _flush_input():
    """清空 UART 接收缓冲区中的残留数据"""
    if _uart is None:
        return
    discarded = 0
    while _uart.any():
        _uart.read(64)
        discarded += 1
    if discarded:
        log("[串口] 清空残留数据 ({}次)".format(discarded))


def _crc8(data):
    """CRC-8/ATM (poly=0x07)，与 Arduino 帧校验实现一致。"""
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def _build_frame(cmd, sequence):
    core = ("{:04X}:{}".format(sequence & 0xFFFF, cmd)).encode("utf-8")
    return b"@" + core + ("*{:02X}\n".format(_crc8(core))).encode("ascii")


def _decode_frame(response, expected_sequence):
    """返回 (payload_bytes, framed, error)。旧版裸 JSON 会原样返回。"""
    if not response.startswith(b"@"):
        return response, False, None
    star = response.rfind(b"*")
    colon = response.find(b":", 1)
    if star < 0 or colon < 0 or star <= colon or len(response) - star != 3:
        return None, True, "响应帧格式错误"
    core = response[1:star]
    try:
        sequence = int(response[1:colon], 16)
        received_crc = int(response[star + 1:], 16)
    except ValueError:
        return None, True, "响应帧序号或校验码无效"
    if sequence != (expected_sequence & 0xFFFF):
        return None, True, "响应帧序号不匹配"
    if received_crc != _crc8(core):
        return None, True, "响应帧 CRC 错误"
    return response[colon + 1:star], True, None


def _uart_write_complete(payload):
    """完整写入并等待 UART TX FIFO 发完，避免命令尾字节滞留。"""
    total = 0
    while total < len(payload):
        written = _uart.write(payload[total:])
        if written is None:
            total = len(payload)
            break
        if written <= 0:
            break
        total += written
    try:
        _uart.flush()
    except Exception:
        # 兼容没有 UART.flush() 的旧 MicroPython；按 10bit/byte 留足时间。
        delay_ms = max(2, (len(payload) * 10000 + config.UART_BAUD - 1) // config.UART_BAUD)
        cooperative.sleep_ms(delay_ms)
    return total


def _send_and_receive(cmd, timeout_ms=0, sequence=0, use_frame=True):
    """
    发送一条指令并等待 JSON 响应。
    timeout_ms: 自定义超时(ms)，0 表示使用 config 默认值。
    返回解析后的 dict，或错误时返回 {"ok": False, "error": "..."}。
    """
    if _uart is None:
        return {"ok": False, "error": "UART 未初始化"}

    _flush_input()
    cooperative.sleep_ms(50)
    _flush_input()

    payload = _build_frame(cmd, sequence) if use_frame else (cmd + "\n").encode()
    written = _uart_write_complete(payload)
    protocol = "校验帧" if use_frame else "兼容行"
    log("[串口] 发送: {} ({}, {}/{} 字节)".format(
        cmd, protocol, written, len(payload)))
    if written != len(payload):
        return {"ok": False, "error": "UART 指令未完整发送"}

    if timeout_ms <= 0:
        timeout_ms = config.UART_TIMEOUT_MS

    buf = bytearray(384)
    pos = 0
    start_ms = time.ticks_ms()

    while time.ticks_diff(time.ticks_ms(), start_ms) < timeout_ms:
        if _uart.any():
            ch = _uart.read(1)
            if ch is None:
                continue
            b = ch[0]
            if b == 0x0A or b == 0x0D:
                if pos > 0:
                    break
                continue
            if pos < len(buf):
                buf[pos] = b
                pos += 1
        else:
            cooperative.sleep_ms(2)

    elapsed = time.ticks_diff(time.ticks_ms(), start_ms)

    if pos == 0:
        log("[串口] 响应超时 ({}ms/{}ms)".format(elapsed, timeout_ms))
        return {"ok": False, "error": "响应超时"}

    response = bytes(buf[:pos])
    decoded, framed, frame_error = _decode_frame(response, sequence)
    if frame_error:
        log("[串口] {}".format(frame_error))
        return {"ok": False, "error": frame_error}
    if decoded is not None:
        response = decoded

    try:
        text = response.decode("utf-8").strip()
    except Exception:
        hex_preview = " ".join("{:02x}".format(b) for b in response[:40])
        log("[串口] 非 UTF-8 数据 ({}ms, {}字节) 十六进制: {}".format(
            elapsed, pos, hex_preview))
        return {"ok": False, "error": "非 UTF-8 响应"}

    log("[串口] 收到 ({}ms, {}): {}".format(
        elapsed, "校验帧" if framed else "兼容行", text))
    try:
        result = json.loads(text)
        result["_framed"] = framed
        if use_frame and not framed and str(result.get("error", "")).startswith(
                "unknown command: @"):
            result["_frame_unsupported"] = True
        return result
    except ValueError:
        log("[串口] JSON 解析失败: {}".format(text[:80]))
        return {"ok": False, "error": "JSON 解析失败"}


# ---- 超时分档（2026-10-02 真机实测）---------------------------------
# 实测值（`tools/motion_timing_probe.py`）：
#   GetStatus 102ms | Move(0,0)->(2200,1440) 1.24s | 回程 2.22s | 1 步 90ms
#   ZClick 1.41s | MoveClick@Home 1.43s | Drag 微幅 1.40s
# 结论：**没有一个运动动作接近 20 秒** —— 旧的 20000 是实测最坏值的 ~9 倍。
# 分档一律取"实测最坏值 × ~4"，并**全部受硬上限约束**：一次超时的真实代价是
# **阻塞主循环 → MQTT 心跳饿死 → 云端看到"设备莫名离线"**，所以宁可早失败、不要长等。
TIMEOUT_STATUS_MS = 3000
TIMEOUT_MOVE_MS = 10000
TIMEOUT_CLICK_BASE_MS = 8000      # 行程 + Z 轴下压/抬起 + 余量（实测 ~1.4s）
TIMEOUT_DRAG_MS = 10000
TIMEOUT_MOTION_MAX_MS = 15000     # 硬上限：任何运动类分档都不得越过
TIMEOUT_HOME_MS = 60000           # Z 轴无限位开关时归零需 ~50s（不受硬上限约束）
TIMEOUT_MOTION_MS = TIMEOUT_MOVE_MS   # 兼容旧名
_CMD_MAX_RETRIES = 1


def _clamp_motion_timeout(value):
    """运动类超时统一受硬上限约束 —— 分档值怎么算都不该把主循环堵得太久。"""
    return min(int(value), TIMEOUT_MOTION_MAX_MS)


def _click_timeout(hold_ms):
    """点击类超时 = 基础（行程 + Z 轴下压/抬起）+ 保持时长，再受硬上限约束。

    为什么必须加 `hold_ms`：`long_press` 的 `duration_ms`、`raw_z_click` 的 `hold_ms`
    最大到 5000 —— 固定超时会在"按得久"时被**误判成超时**（`z_click` 原先走默认
    `config.UART_TIMEOUT_MS = 3000`，配 5000ms 的 hold 必然误判）。
    """
    return _clamp_motion_timeout(TIMEOUT_CLICK_BASE_MS + max(0, int(hold_ms or 0)))


# "没拿到可用答复"的错误 = 值得重试（答复可能只是丢了）；
# **明确失败**（下位机给了答复，例如 `duplicate previous command failed`）不在其列。
_LOST_RESPONSE_ERRORS = (
    "响应超时", "非 UTF-8 响应", "JSON 解析失败", "UART 指令未完整发送",
)


def _is_lost_response(response):
    """这次是"没拿到可用答复"吗？

    只有这种才重试。理由：重试**复用同一序号**，
    而下位机**按序号判重** —— 对明确失败再重试，拿回的是 `duplicate previous command failed`
    这条**回声**，把第一次的真实原因盖掉，模型只能盲试。
    """
    if not isinstance(response, dict):
        return False
    error = response.get("error") or ""
    # "响应帧格式错误/序号不匹配/CRC 错误"= 收到的是残帧或陈旧帧，等效于答复丢失。
    return error in _LOST_RESPONSE_ERRORS or error.startswith("响应帧")


def _annotate_first_failure(response, first_failure, attempts):
    """把**第一次尝试**的回执附到最终失败上，别让重试的结果盖掉首因。

    为什么：重试**复用同一序号**，而下位机**按序号判重** ——
    对"已经用过的序号"它会回 `duplicate previous command failed`。于是**最终错误是重试的回声**，
    第一次究竟为什么失败（超时？下位机真报错？）**被盖掉**，模型只拿到一个看不懂的错误、
    只能盲试。附上 `first_attempt` 后，`driver_response` 里**首因与回声同时可见**。

    **只在真的重试过（`attempts > 1`）时才附** —— 否则会把一次尝试谎报成两次。
    """
    if (attempts > 1 and isinstance(first_failure, dict)
            and isinstance(response, dict) and "first_attempt" not in response):
        response["first_attempt"] = first_failure
        response["attempts"] = attempts
    return response


def send_command(cmd, timeout_ms=0):
    """
    发送指令并等待响应, 失败时自动重试 1 次。
    硬件串口通信可靠性高, 重试仅作为安全网。
    """
    global _next_sequence, _framing_supported
    sequence = _next_sequence
    _next_sequence = (_next_sequence + 1) & 0xFFFF
    use_frame = _framing_supported is not False
    first_failure = None
    attempts = 0

    for attempt in range(_CMD_MAX_RETRIES + 1):
        attempts += 1
        response = _send_and_receive(
            cmd, timeout_ms, sequence=sequence, use_frame=use_frame)
        if response.pop("_frame_unsupported", False):
            _framing_supported = False
            use_frame = False
            log("[串口] 下位机为旧版协议，本次启动回退到兼容行模式")
            response = _send_and_receive(
                cmd, timeout_ms, sequence=sequence, use_frame=False)
        elif response.pop("_framed", False):
            _framing_supported = True

        if response.get("ok") or response.get("error") == "UART 未初始化":
            return response
        if attempt == 0:
            first_failure = dict(response) if isinstance(response, dict) else None
        if attempt < _CMD_MAX_RETRIES:
            # **早失败**：下位机已经给了明确答复（不是"答复丢失"）就立刻返回，不再重试。
            # 重试只会复用同一序号、拿回 `duplicate previous command failed` 的回声，
            # 把第一次的真实原因盖掉 —— 那不是"再试一次"，是"换一个更难懂的错误"。
            if not _is_lost_response(response):
                return _annotate_first_failure(response, first_failure, attempts)
            if not use_frame and cmd != "GetStatus":
                # 行协议无去重保障：超时结果未知，不自动再次运动。
                return _annotate_first_failure(response, first_failure, attempts)
            cooperative.sleep_ms(50)
            if use_frame:
                log("[串口] 响应丢失，复用同一序号重试（下位机会回 duplicate 而不重复执行）...")
            else:
                log("[串口] 兼容行模式重试...")
    return _annotate_first_failure(response, first_failure, attempts)


def move(x_steps, y_steps):
    """发送 Move 指令 (步进数)"""
    return send_command("Move,{},{}".format(int(x_steps), int(y_steps)),
                        _clamp_motion_timeout(TIMEOUT_MOVE_MS))


def _click_hold_ms(delay_ms):
    """解析普通 Z 点击保持时间；显式值用于 long_press 等特殊动作。"""
    if delay_ms is None:
        delay_ms = getattr(config, "Z_CLICK_HOLD_MS", 250)
    return max(1, int(delay_ms))


def move_click(x_ratio, y_ratio, click_count=1, delay_ms=None):
    """
    发送 MoveClick 指令 (比例坐标 0.0~1.0)。
    v6 协议约定: (0,0) 为手机左上侧笔尖 Home，x_ratio 向手机右边增大，
    y_ratio 向手机底部增大。
    下位机必须将 Home 限位和正向运动配置成同一物理含义。
    click_count: 点击次数。
    delay_ms: 笔尖到达最低点后的保持时间；多次点击时也作为点击间隔。
              None 使用 config.Z_CLICK_HOLD_MS，普通点击当前为 250ms。
    """
    return send_command("MoveClick,{:.4f},{:.4f},{},{}".format(
        float(x_ratio), float(y_ratio), int(click_count),
        _click_hold_ms(delay_ms)),
        _click_timeout(_click_hold_ms(delay_ms)))


def drag(start_x, start_y, end_x, end_y):
    """
    发送 Drag 指令 (比例坐标 0.0~1.0)。
    坐标方向同 MoveClick: Home 位于手机左上侧，+X 左→右，+Y 上→下。
    从 (start_x, start_y) 拖拽到 (end_x, end_y)。
    """
    return send_command("Drag,{:.4f},{:.4f},{:.4f},{:.4f}".format(
        float(start_x), float(start_y), float(end_x), float(end_y)),
        _clamp_motion_timeout(TIMEOUT_DRAG_MS))


def z_click(click_count=1, delay_ms=None):
    """发送 ZClick 指令；默认使用可靠的普通点击保持时间。

    超时按点击类分档（原先走默认 `config.UART_TIMEOUT_MS = 3000`）——
    `raw_z_click` 允许 `hold_ms` 到 5000，固定 3 秒必然把"按得久"误判成超时。
    """
    return send_command("ZClick,{},{}".format(
        int(click_count), _click_hold_ms(delay_ms)),
        _click_timeout(_click_hold_ms(delay_ms)))


def home():
    """发送 Home 指令 (三轴归零)"""
    return send_command("Home", TIMEOUT_HOME_MS)


def get_status():
    """发送 GetStatus 查询当前位置与限位状态"""
    return send_command("GetStatus", TIMEOUT_STATUS_MS)

