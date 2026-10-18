"""共享测试替身。

真机驱动绑 `machine` / `neopixel` / `network`，桌面跑不了，所以按接口造替身。
**同形的替身才是有效的替身** —— 真机某处返回 tuple 而不是 list、或抛的是另一类
异常时，替身必须跟着同形，否则那条 bug 在替身上永远测不出来（见 `FakeCameraDriver`
里那段注释）。
"""


class FakeMotionDriver:
    def __init__(self):
        self.calls = []

    def begin(self):
        self.calls.append(("begin",))

    def get_status(self):
        self.calls.append(("status",))
        return {"ok": True, "x_steps": 0, "y_steps": 0, "z_steps": 0}

    def home(self):
        self.calls.append(("home",))
        return {"ok": True, "homed": True}

    def move(self, x, y):
        self.calls.append(("move", x, y))
        return {"ok": True}

    def move_click(self, x, y, count=1, delay_ms=None):
        self.calls.append(("move_click", x, y, count, delay_ms))
        return {"ok": True}

    def drag(self, sx, sy, ex, ey):
        self.calls.append(("drag", sx, sy, ex, ey))
        return {"ok": True}

    def z_click(self, count=1, delay_ms=None):
        self.calls.append(("z_click", count, delay_ms))
        return {"ok": True}


class FakeCameraDriver:
    SPECS = {
        "quality": ("int", 10, 20), "brightness": ("int", -2, 2),
        "contrast": ("int", -2, 2), "sharpness": ("int", -3, 3),
    }

    def __init__(self):
        self.ready = False
        self.settings = {"quality": 10, "brightness": 0,
                         "contrast": 1, "sharpness": 3}
        self.saved = None
        self.frames = [b"\xff\xd8fake-jpeg\xff\xd9"]

    def begin(self):
        self.ready = True
        return True

    def is_ready(self):
        return self.ready

    def get_frame_size(self):
        return 1600, 1200

    def capture(self):
        return self.frames[0] if self.frames else None

    def get_camera_settings(self):
        return dict(self.settings)

    def camera_setting_specs(self):
        # 和真机驱动一样返回 list 而不是 tuple：真机这里返回 tuple 曾经把命令
        # 日志的读回比对骗成"写失败"，把整台设备锁死（见 drivers/camera_manager.py
        # 的注释与对应的回归测试）。替身必须同形，否则这条 bug 在替身上永远测不出来。
        return {name: list(spec) for name, spec in self.SPECS.items()}

    def camera_profile_exists(self):
        return self.saved is not None

    def safe_camera_settings(self):
        return {"quality": 20, "brightness": 0,
                "contrast": 1, "sharpness": 3}

    def diagnostic_camera_settings(self):
        # 「诊断档」：与 safe 相反的一档（锁曝光/锁增益/不加锐/小字清楚），
        # 替身只需要"与 safe 明显不同"，好把"切换真的发生了"测出来。
        return {"quality": 10, "brightness": 0,
                "contrast": 1, "sharpness": -3}

    def apply_camera_settings(self, values):
        for key, value in values.items():
            if key not in self.SPECS:
                raise ValueError("unsupported setting")
            low, high = self.SPECS[key][1:]
            if int(value) < low or int(value) > high:
                raise ValueError("out of range")
            self.settings[key] = int(value)
        return dict(self.settings)

    def save_camera_profile(self, values):
        self.saved = dict(values)
        return dict(self.saved)

    def load_camera_profile(self, refresh=False):
        return dict(self.saved) if self.saved else None

    def deinit(self):
        self.ready = False


class FakeWifi:
    """STA 侧：连接、扫描与凭据。热点生命周期见 `FakeAp`。"""

    def __init__(self, connected=True):
        self.connected = connected
        self.credentials = None
        self.saved_available = True
        self.reconnect_started = False
        self.reconnect_polls = []

    def connect_saved(self):
        return self.connected

    def is_connected(self):
        return self.connected

    def get_ip(self):
        return "192.168.1.20" if self.connected else "0.0.0.0"

    def get_ssid(self):
        return "test-wifi" if self.connected else ""

    def scan_networks(self):
        return [{"ssid": "test-wifi", "rssi": -40, "secure": True}]

    def save_credentials(self, ssid, password):
        self.credentials = (ssid, password)
        self.saved_available = True

    def clear_credentials(self):
        self.credentials = None
        self.saved_available = False
        self.connected = False
        return True

    def start_saved_reconnect(self):
        if self.connected:
            return {"started": False, "reason": "already_connected"}
        if not self.saved_available:
            return {"started": False, "reason": "no_saved_credentials"}
        self.reconnect_started = True
        return {"started": True, "ssid": "test-wifi"}

    def poll_saved_reconnect(self):
        result = (self.reconnect_polls.pop(0) if self.reconnect_polls
                  else {"state": "connecting"})
        if result.get("state") == "connected":
            self.connected = True
            self.reconnect_started = False
        elif result.get("state") == "failed":
            self.reconnect_started = False
        return result

    def cancel_saved_reconnect(self):
        self.reconnect_started = False


class FakeAp:
    """AP 侧：配网热点的起停与地址。"""

    def __init__(self, ip="192.168.4.1"):
        self.ip = ip
        self.started = 0
        self.stopped = 0

    def start_ap(self):
        self.started += 1
        return True

    def stop_ap(self):
        self.stopped += 1
        return True

    def get_ap_ip(self):
        return self.ip


class FakeButtons:
    def __init__(self):
        self.events = []

    def start(self):
        return True

    def stop(self):
        pass

    def poll(self):
        pass

    def consume(self):
        events, self.events = self.events, []
        return events


class FakeLed:
    def __init__(self):
        self.states = []

    def start(self):
        return True

    def set(self, state):
        self.states.append(state)

    def stop(self):
        pass
