"""MicroPython / CPython 最小兼容层。"""

try:
    import ujson as json
except ImportError:  # pragma: no cover - CPython
    import json

try:
    import ubinascii as binascii
except ImportError:  # pragma: no cover - CPython
    import binascii

try:
    import utime as time
except ImportError:  # pragma: no cover - CPython
    import time


def ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(new, old):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(new, old)
    return new - old


def ticks_add(value, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(value, int(delta))
    return value + int(delta)


def sleep_ms(value):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(int(value))
    else:
        time.sleep(float(value) / 1000.0)


def epoch_ms():
    """统一 Unix epoch；部分 MicroPython 固件采用 2000 epoch。"""
    offset = 946684800 if time.gmtime(0)[0] == 2000 else 0
    return int((time.time() + offset) * 1000)


def hexlify(value, sep=None):
    """bytes → 十六进制字符串；`sep` 可选，用于 MAC 这类需要分隔的展示。"""
    raw = binascii.hexlify(value) if sep is None else binascii.hexlify(value, sep)
    return raw.decode("ascii") if isinstance(raw, bytes) else str(raw)
