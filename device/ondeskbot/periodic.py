"""`machine.Timer` 周期性回调的薄封装。

状态灯与按键去抖共用同一套「取 Timer → 不可用就降级 → deinit 收尾」的生命周期，
这里只写一遍：固件没编 `Timer` 时 `start()` 返回 False，调用方自行退回主循环轮询。
"""


class PeriodicTimer:
    def __init__(self, machine_module, index, period_ms, callback):
        self._machine = machine_module
        self._index = index
        self._period_ms = period_ms
        self._callback = callback
        self._timer = None

    def start(self):
        """起周期定时器；固件没有 Timer 类时返回 False（不抛错）。"""
        timer_class = getattr(self._machine, "Timer", None)
        if timer_class is None:
            return False
        self._timer = timer_class(self._index)
        self._timer.init(period=self._period_ms,
                         mode=timer_class.PERIODIC, callback=self._callback)
        return True

    def is_running(self):
        return self._timer is not None

    def stop(self):
        if self._timer:
            self._timer.deinit()
            self._timer = None
