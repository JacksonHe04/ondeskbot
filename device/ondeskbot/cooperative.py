"""长操作在等待边界让出控制；回调只能处理通信及安全锁存，不能重入硬件。"""
from .compat import sleep_ms as raw_sleep_ms, ticks_ms, ticks_diff
_hook = None
_running = False


def set_hook(hook):
    global _hook
    _hook = hook


def checkpoint():
    global _running
    if _hook and not _running:
        _running = True
        try:
            _hook()
        finally:
            _running = False


def sleep_ms(delay):
    start = ticks_ms()
    while ticks_diff(ticks_ms(), start) < delay:
        checkpoint()
        raw_sleep_ms(min(20, max(1, delay - ticks_diff(ticks_ms(), start))))
