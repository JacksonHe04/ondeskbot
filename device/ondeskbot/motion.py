"""ArduinoDriverSCurveXY 的安全上层动作。"""

import config

from . import coordinate_transform as coord
from .errors import DeviceError, INVALID_ARGUMENT, MOTION_FAILED
from . import compat
from .log import log

# Z 归零检测是**边缘性**的：2026-10-02 真机实测同一台机器上，
# `Home` 第一次回 `Z Home limit not reached`、**紧接着第二次就成功**。
# 现场事实与之相容：笔头曾脱落、装回后 Z 的行程/负载变了，检测落在阈值边缘。
_TRANSIENT_HOME_ERRORS = ("Z Home limit not reached",)

# 未归零自动恢复（；开关见 config.MOTION_RECOVER_UNHOMED，默认 **True**）。
# 白名单：**只认这三条下位机原文**属于"未归零"族，才允许置位触发恢复。
# 2026-10-03 R20/R21/R23/R25 四轮实测：触发串都是"**横向拖动**后 Z 没回到限位"，
# 后续那些 `XYZ not homed` / `Z not homed` 全是**联锁回声**（不是独立故障）。
_UNHOMED_DRIVER_ERRORS = (
    "Z home switch not reached after drag",
    "XYZ not homed",
    "Z not homed",
)


def _driver_error_text(error):
    """从 `MOTION_FAILED` 的 details 里取出**下位机原文**（`driver_response.error`）。"""
    response = (getattr(error, "details", None) or {}).get("driver_response")
    if isinstance(response, dict):
        return str(response.get("error") or "")
    return ""


def _ratio(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise DeviceError(INVALID_ARGUMENT, "{} 必须是 0..1 的数字".format(name))
    if number < 0 or number > 1:
        raise DeviceError(INVALID_ARGUMENT, "{} 超出 0..1".format(name))
    return number


def _require_ok(response, operation):
    if not isinstance(response, dict) or not response.get("ok"):
        raise DeviceError(MOTION_FAILED, "{} 失败".format(operation), {
            "driver_response": response or {},
        }, retryable=False)
    return response


class MotionController:
    def __init__(self, driver, calibration):
        self.driver = driver
        self.calibration = calibration
        # 「未归零 ⇒ 下一次动作前先归零」的状态
        self._needs_home = False
        self._recovery_disabled = False
        self._recoveries_used = 0
        self._last_recovery_ms = None

    # ---- 未归零自动恢复（，默认关闭，config.MOTION_RECOVER_UNHOMED）--------
    def reset_task_state(self):
        """任务起/止时调用（`service` 负责）：恢复计数与禁用位归零。"""
        self._needs_home = False
        self._recovery_disabled = False
        self._recoveries_used = 0
        self._last_recovery_ms = None

    def recovery_enabled(self):
        return bool(getattr(config, "MOTION_RECOVER_UNHOMED", False))

    def recovery_state(self):
        """给 diag / 回执用的只读快照。"""
        return {"needs_home": self._needs_home,
                "disabled": self._recovery_disabled,
                "used": self._recoveries_used,
                "enabled": self.recovery_enabled()}

    def _ensure_homed(self):
        """**需要 Z 的动作开始前**：若上一批动作把设备打成"未归零"，先归零再执行本动作。

        为什么这么设计："失败后补救"会丢一步；"**下一次动作前先归零**"
        ⇒ **一步不丢**（动作照样执行），且**级联根本不发生**（触发它的 `z_homed=0`
        在本动作开始前就被清掉）。而 `tap`/`swipe` 等用的是**绝对**标定坐标
        （`screen_to_motor`）⇒ **先归零不改变动作落点** ✓。

        返回 `"home_before_motion"` 表示本次恢复过（会被记进动作回执，云端可统计）。
        """
        if not (self.recovery_enabled() and self._needs_home) or self._recovery_disabled:
            return None
        if self._recoveries_used >= int(getattr(config, "MOTION_RECOVERY_LIMIT", 2)):
            # 闸 3：任务级上限；用满即停（不再恢复，让失败照常暴露）
            self._recovery_disabled = True
            log("[机械] 未归零恢复次数已达上限，本任务内不再恢复")
            return None
        # **冷却**：两次自动归零之间至少间隔 `MOTION_RECOVERY_COOLDOWN_MS`。
        # 这次不够间隔就**不恢复、照常失败**（但**不禁用** —— 下次够间隔再恢复 ✓）。
        # 为什么几乎不额外限制：每次恢复本身就要 30–45 s ⇒ 正常节奏下天然满足 ✓。
        cooldown_ms = int(getattr(config, "MOTION_RECOVERY_COOLDOWN_MS", 0))
        now_ms = compat.ticks_ms()
        if (cooldown_ms > 0 and self._last_recovery_ms is not None and
                compat.ticks_diff(now_ms, self._last_recovery_ms) < cooldown_ms):
            log("[机械] 距上次自动归零不足 {} ms，本次不恢复".format(cooldown_ms))
            return None
        log("[机械] 检测到未归零，先归零再执行本动作")
        try:
            self.home({})
        except Exception as error:
            # 闸 2：**恢复用的 Home 自己失败 ⇒ 禁用恢复** ⇒ 断掉"恢复失败再失败"的循环
            self._recovery_disabled = True
            log("[机械] 恢复归零失败，本任务内禁用恢复: {}".format(error))
            raise
        self._needs_home = False
        self._recoveries_used += 1
        self._last_recovery_ms = now_ms
        return "home_before_motion"

    def _motion(self, operation, fn):
        """需要 Z 的动作统一走这里：包住 driver 失败，识别"未归零"族并**置位**。

        **置位不等于恢复** —— 本动作仍按原样失败上报（不掩护、不改判），
        恢复发生在**下一个**需要 Z 的动作之前（见 `_ensure_homed`）。
        """
        try:
            return _require_ok(fn(), operation)
        except DeviceError as error:
            if _driver_error_text(error) in _UNHOMED_DRIVER_ERRORS:
                self._needs_home = True
            raise

    @staticmethod
    def _with_recovery(payload, recovered):
        if recovered:
            payload["recovered"] = recovered
        return payload

    def begin(self):
        self.driver.begin()
        return self.status({})

    def status(self, _args=None):
        return _require_ok(self.driver.get_status(), "查询机械状态")

    def home(self, _args=None):
        """机械归零。**Z 归零检测是边缘性的** ⇒ 有界重试一次，并把"重试过"记进回执。

        为什么非要重试：`Home` 落在**关键路径**上 ——
        云端 `task.confirm` 之后、任务转 active 之前会先归零
        （`service._finish_confirmation`），**归零失败会清任务并发出
        `task.confirmation_failed(home_failed)`** ⇒ 一次**边缘性**失败就能废掉一整轮 run ✗。
        而实测同一台机器"失败一次、重试即成"⇒ 值得一次有界重试。

        **只重试一次、且不静默**：既不掩盖"开关真坏了"（仍会失败），
        也不把"重试过"藏起来（成功回执里带 `home_retried=True`）。
        """
        try:
            return _require_ok(self.driver.home(), "机械归零")
        except DeviceError as error:
            text = _driver_error_text(error)
            if error.code != MOTION_FAILED or text not in _TRANSIENT_HOME_ERRORS:
                raise
            log("[机械] 归零失败（{}），重试一次 —— Z 归零检测是边缘性的".format(text))
            result = _require_ok(self.driver.home(), "机械归零(重试)")
            result["home_retried"] = True
            return result

    def tap(self, args):
        x = _ratio(args.get("x"), "x")
        y = _ratio(args.get("y"), "y")
        mx, my = self.calibration.screen_to_motor(x, y, click_offset=True)
        recovered = self._ensure_homed()
        result = self._motion("点击", lambda: self.driver.move_click(mx, my, 1))
        return self._with_recovery(
            {"screen": {"x": x, "y": y}, "motor": {"x": mx, "y": my},
             "driver": result}, recovered)

    def double_tap(self, args):
        x = _ratio(args.get("x"), "x")
        y = _ratio(args.get("y"), "y")
        mx, my = self.calibration.screen_to_motor(x, y, click_offset=True)
        recovered = self._ensure_homed()
        result = self._motion("双击", lambda: self.driver.move_click(
            mx, my, 2, config.Z_DOUBLE_TAP_HOLD_MS))
        return self._with_recovery(
            {"screen": {"x": x, "y": y}, "motor": {"x": mx, "y": my},
             "driver": result}, recovered)

    def long_press(self, args):
        x = _ratio(args.get("x"), "x")
        y = _ratio(args.get("y"), "y")
        duration = int(args.get("duration_ms", config.LONG_PRESS_HOLD_MS))
        if duration < 500 or duration > 5000:
            raise DeviceError(INVALID_ARGUMENT, "duration_ms 必须位于 500..5000")
        mx, my = self.calibration.screen_to_motor(x, y, click_offset=True)
        recovered = self._ensure_homed()
        result = self._motion("长按", lambda: self.driver.move_click(mx, my, 1, duration))
        return self._with_recovery(
            {"screen": {"x": x, "y": y}, "duration_ms": duration,
             "motor": {"x": mx, "y": my}, "driver": result}, recovered)

    def swipe(self, args):
        direction = coord.normalize_swipe_direction(args.get("direction", ""))
        path = coord.swipe_screen_path(
            direction, config.SWIPE_START_RATIO, config.SWIPE_END_RATIO)
        if not path:
            raise DeviceError(INVALID_ARGUMENT,
                              "direction 必须是 up/down/left/right")
        (sx, sy), (ex, ey) = path
        msx, msy = self.calibration.screen_to_motor(sx, sy)
        mex, mey = self.calibration.screen_to_motor(ex, ey)
        recovered = self._ensure_homed()
        result = self._motion("滑动", lambda: self.driver.drag(msx, msy, mex, mey))
        return self._with_recovery({
            "direction": direction,
            "screen_start": {"x": sx, "y": sy},
            "screen_end": {"x": ex, "y": ey},
            "motor_start": {"x": msx, "y": msy},
            "motor_end": {"x": mex, "y": mey},
            "driver": result,
        }, recovered)

    def raw_move(self, args):
        try:
            x_steps = int(args.get("x_steps"))
            y_steps = int(args.get("y_steps"))
        except (TypeError, ValueError):
            raise DeviceError(INVALID_ARGUMENT, "x_steps/y_steps 必须是整数")
        if not (0 <= x_steps <= config.RAW_MOVE_MAX_X_STEPS):
            raise DeviceError(INVALID_ARGUMENT, "x_steps 超出维护安全范围")
        if not (0 <= y_steps <= config.RAW_MOVE_MAX_Y_STEPS):
            raise DeviceError(INVALID_ARGUMENT, "y_steps 超出维护安全范围")
        return _require_ok(self.driver.move(x_steps, y_steps), "原始 XY 移动")

    def raw_z_click(self, args):
        count = int(args.get("count", 1))
        hold_ms = int(args.get("hold_ms", config.Z_CLICK_HOLD_MS))
        if count < 1 or count > 2:
            raise DeviceError(INVALID_ARGUMENT, "count 只允许 1 或 2")
        if hold_ms < 20 or hold_ms > 5000:
            raise DeviceError(INVALID_ARGUMENT, "hold_ms 必须位于 20..5000")
        recovered = self._ensure_homed()
        result = self._motion("原始 Z 点击", lambda: self.driver.z_click(count, hold_ms))
        return self._with_recovery(result, recovered)

