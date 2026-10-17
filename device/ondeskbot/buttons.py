"""BTN1/BTN2/BTN3 事件采集；Timer 回调只锁存标志，不执行 I/O。"""

import config

from .compat import ticks_diff, ticks_ms
from .periodic import PeriodicTimer


class ButtonManager:
    def __init__(self, machine_module=None, now_provider=None):
        if machine_module is None:
            import machine as machine_module
        self.machine = machine_module
        self._now = now_provider or ticks_ms
        pull_up = getattr(self.machine.Pin, "PULL_UP", None)
        mode = getattr(self.machine.Pin, "IN", None)
        self.dev = self.machine.Pin(config.BUTTON_DEV_PIN, mode, pull_up)
        self.reprovision = self.machine.Pin(
            config.BUTTON_REPROVISION_PIN, mode, pull_up)
        self.pause = self.machine.Pin(config.BUTTON_PAUSE_PIN, mode, pull_up)
        self.confirm = self.machine.Pin(config.BUTTON_CONFIRM_PIN, mode, pull_up)
        self._previous_pause = self.pause.value()
        self._previous_confirm = self.confirm.value()
        self._reprovision_started = 0
        self._reprovision_latched = False
        self._pause_pending = False
        self._confirm_pending = False
        self._reprovision_pending = False
        self._timer = PeriodicTimer(
            self.machine, 1, config.BUTTON_DEBOUNCE_MS, self._sample)

    def _pressed(self, pin):
        return pin.value() == config.BUTTON_ACTIVE_LEVEL

    def _sample(self, _timer=None):
        now = self._now()
        pause_value = self.pause.value()
        confirm_value = self.confirm.value()
        if (self._previous_pause != config.BUTTON_ACTIVE_LEVEL and
                pause_value == config.BUTTON_ACTIVE_LEVEL):
            self._pause_pending = True
        if (self._previous_confirm != config.BUTTON_ACTIVE_LEVEL and
                confirm_value == config.BUTTON_ACTIVE_LEVEL):
            self._confirm_pending = True
        self._previous_pause = pause_value
        self._previous_confirm = confirm_value

        if self._pressed(self.reprovision):
            if not self._reprovision_started:
                self._reprovision_started = now
            elif (not self._reprovision_latched and
                  ticks_diff(now, self._reprovision_started) >=
                  config.BUTTON_REPROVISION_HOLD_MS):
                self._reprovision_latched = True
                self._reprovision_pending = True
        else:
            self._reprovision_started = 0
            self._reprovision_latched = False

    def start(self):
        return self._timer.start()

    def is_running(self):
        return self._timer.is_running()

    def stop(self):
        self._timer.stop()

    def poll(self):
        if not self._timer.is_running():
            self._sample()

    def consume(self):
        events = []
        if self._reprovision_pending:
            self._reprovision_pending = False
            events.append("reprovision")
        if self._pause_pending:
            self._pause_pending = False
            events.append("pause_toggle")
        if self._confirm_pending:
            self._confirm_pending = False
            events.append("confirm")
        return events
