"""单颗 WS2812 状态灯。"""

import config

from .periodic import PeriodicTimer

# 闪烁半周期：够慢到看得清，够快到不拖慢对状态迁移的判读。
BLINK_PERIOD_MS = 500

LED_STATES = {
    "off": ((0, 0, 0), False),
    "booting": ((32, 18, 0), True),
    "provisioning": ((0, 32, 0), True),
    "cloud_connecting": ((0, 0, 32), True),
    "offline": ((32, 8, 0), True),
    "idle": ((0, 32, 0), False),
    "awaiting_confirmation": ((0, 0, 48), True),
    "executing": ((0, 32, 32), True),
    "paused": ((32, 20, 0), False),
    "setup": ((0, 24, 32), False),
    "maintenance": ((24, 0, 32), False),
    "error": ((48, 0, 0), False),
}


class StatusLed:
    def __init__(self, machine_module=None, neopixel_module=None):
        if machine_module is None:
            import machine as machine_module
        if neopixel_module is None:
            import neopixel as neopixel_module
        self.machine = machine_module
        self.pixel = neopixel_module.NeoPixel(
            self.machine.Pin(config.LED_PIN), 1)
        self.state = "off"
        self._on = False
        self._timer = PeriodicTimer(self.machine, 3, BLINK_PERIOD_MS, self._tick)
        self.set("off")

    def _write(self, color):
        self.pixel[0] = color
        self.pixel.write()

    def _tick(self, _timer=None):
        color, blink = LED_STATES.get(self.state, LED_STATES["error"])
        if blink:
            self._on = not self._on
            self._write(color if self._on else (0, 0, 0))
        else:
            self._on = True
            self._write(color)

    def start(self):
        return self._timer.start()

    def set(self, state):
        self.state = state if state in LED_STATES else "error"
        self._on = False
        self._tick()

    def stop(self):
        self._timer.stop()
        self._write((0, 0, 0))
