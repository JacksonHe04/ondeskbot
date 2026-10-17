"""任务、暂停、取消和维护会话的安全状态机。"""

import config

from .compat import epoch_ms, ticks_add, ticks_diff, ticks_ms
from .errors import (
    DEVICE_BUSY,
    MAINTENANCE_CONFIRMATION_REQUIRED,
    MAINTENANCE_SESSION_REQUIRED,
    TASK_CONFIRMATION_REQUIRED,
    TASK_NOT_ACTIVE,
    TASK_PAUSED,
    DeviceError,
)


class RuntimeState:
    def __init__(self, now_provider=None):
        self._now = now_provider or ticks_ms
        self.phase = "booting"
        self.connected = False
        self.connectivity_state = "offline"
        self.last_disconnect = None
        self.last_connection_failure = None
        self.reconnect_count = 0
        self._ever_connected = False
        self.busy = False
        self.current_capability = ""
        self.task_id = ""
        self.task_status = "none"  # none|pending|active|pause_pending|paused|cancel_pending
        self.task_deadline_ms = 0
        # 最近一次**正常结束/取消**的 task_id（供 task.end / task.cancel 幂等判重）。
        #
        # 为什么需要：云端 Agent 的 finish 工具在没读到"成功"时会**重发一次** task.end；
        # 而 task.end 原本不幂等 ⇒ 第二次必然回 `TASK_NOT_ACTIVE` ⇒ Agent 把"成功后的
        # 重发"读成"terminate 失败"，整轮 run 判 failed。
        # 只对**完全同号**的重复调用放行；不同 task_id 一律照旧报错，不掩盖真错。
        self.last_closed_task_id = ""
        self.maintenance_id = ""
        self.maintenance_status = "none"  # none|pending|active
        self.maintenance_deadline_ms = 0
        self.last_error = None
        # 云端 task.confirm 已经翻完状态、但"归零 + 发事件"的收尾还没做：
        # 命令处理里不能做归零（那会把回执拖到几十毫秒之后），交给主循环用
        # 与本地按键确认相同的收尾段完成。
        self.cloud_confirmed = None

    def set_phase(self, phase):
        self.phase = str(phase)

    def set_connected(self, value, reason="", details=None):
        """记录设备本地看到的云连接状态与最近一次断线诊断。

        suspected_offline 是 Server 基于 last_seen 计算的状态；设备本地一旦确认
        Transport 不可用就直接记录 offline。
        """
        value = bool(value)
        if value:
            if not self.connected and self._ever_connected:
                self.reconnect_count += 1
            self.connected = True
            self.connectivity_state = "online"
            self._ever_connected = True
            return

        was_connected = self.connected
        self.connected = False
        self.connectivity_state = "offline"
        if reason:
            item = {
                "reason": str(reason),
                "observed_at_ms": epoch_ms(),
                "details": details or {},
            }
            if was_connected:
                self.last_disconnect = item
            else:
                self.last_connection_failure = item
        if was_connected:
            # 网络恢复不恢复动作；需要本地按键主动继续。正在运动则先安全收尾。
            if self.task_status == "active":
                self.task_status = "pause_pending" if self.busy else "paused"
            elif self.task_status == "pending":
                self.clear_task()
            self.clear_maintenance()

    def connectivity_snapshot(self):
        return {
            "state": self.connectivity_state,
            "connected": self.connected,
            "reconnect_count": self.reconnect_count,
            "last_disconnect": self.last_disconnect,
            "last_connection_failure": self.last_connection_failure,
        }

    def record_prior_disconnect(self, reason, details=None):
        """记录本次进程启动前的重启/掉电线索，供重连后上报。"""
        self.last_disconnect = {
            "reason": str(reason),
            "observed_at_ms": epoch_ms(),
            "details": details or {},
        }

    def _deadline_passed(self, deadline):
        return bool(deadline and ticks_diff(self._now(), deadline) >= 0)

    def expire_local_confirmations(self):
        expired = []
        if self.task_status == "pending" and self._deadline_passed(self.task_deadline_ms):
            expired.append(("task", self.task_id))
            self.clear_task()
        if self.maintenance_status == "pending" and self._deadline_passed(
                self.maintenance_deadline_ms):
            expired.append(("maintenance", self.maintenance_id))
            self.clear_maintenance()
        if self.maintenance_status == "active" and self._deadline_passed(
                self.maintenance_deadline_ms):
            expired.append(("maintenance_expired", self.maintenance_id))
            self.clear_maintenance()
        return expired

    def request_task(self, task_id, timeout_s=None):
        task_id = str(task_id or "").strip()
        if not task_id:
            raise DeviceError(TASK_NOT_ACTIVE, "task_id 不能为空")
        if self.task_status != "none" or self.maintenance_status != "none":
            raise DeviceError(DEVICE_BUSY, "设备已有活动任务或维护会话")
        self.task_id = task_id
        self.task_status = "pending"
        self.task_deadline_ms = ticks_add(self._now(), int(
            timeout_s or config.TASK_CONFIRM_TIMEOUT_S) * 1000)
        return self.task_snapshot()

    def take_cloud_confirmed(self):
        """取走云端确认的收尾任务（取一次即清空，避免主循环重复归零）。"""
        value, self.cloud_confirmed = self.cloud_confirmed, None
        return value

    def confirm_pending(self):
        self.expire_local_confirmations()
        if self.task_status == "pending":
            self.task_status = "active"
            self.task_deadline_ms = 0
            return {"kind": "task", "id": self.task_id}
        if self.maintenance_status == "pending":
            self.maintenance_status = "active"
            self.maintenance_deadline_ms = ticks_add(self._now(), int(
                config.MAINTENANCE_SESSION_TTL_S) * 1000)
            return {"kind": "maintenance", "id": self.maintenance_id}
        return None

    def request_pause_toggle(self):
        if self.task_status == "active":
            if self.busy:
                self.task_status = "pause_pending"
                return "after_current_action"
            self.task_status = "pause_pending"
            return "home_now"
        if self.task_status == "pause_pending":
            return "already_pending"
        if self.task_status == "paused":
            self.task_status = "active"
            return "resumed"
        return "ignored"

    def mark_paused(self):
        if self.task_status == "pause_pending":
            self.task_status = "paused"

    def request_cancel(self):
        if self.task_status in ("none", "pending"):
            task_id = self.task_id
            self.clear_task()
            return "cancelled", task_id
        task_id = self.task_id
        self.task_status = "cancel_pending"
        return ("after_current_action" if self.busy else "home_now"), task_id

    def finish_cancel(self):
        task_id = self.task_id
        self.last_closed_task_id = task_id
        self.clear_task()
        return task_id

    def end_task(self):
        task_id = self.task_id
        self.last_closed_task_id = task_id
        self.clear_task()
        return task_id

    def just_closed(self, task_id):
        """该 task_id 是否**刚刚被正常收尾**（结束或取消；task.end / task.cancel 幂等判重用）。

        只认"与最近一次正常结束的同一个号"，且当前必须没有活动任务 ——
        这样重复的 finish 回 `ok:true`，而**不同号**的请求照旧报 TASK_NOT_ACTIVE（不掩盖真错）。
        """
        candidate = str(task_id or "").strip()
        if not candidate:
            return False
        if self.task_status != "none":
            return False
        return candidate == self.last_closed_task_id

    def clear_task(self):
        self.task_id = ""
        self.task_status = "none"
        self.task_deadline_ms = 0

    def request_maintenance(self, session_id, timeout_s=None):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise DeviceError(MAINTENANCE_SESSION_REQUIRED, "session_id 不能为空")
        if self.task_status != "none" or self.maintenance_status != "none":
            raise DeviceError(DEVICE_BUSY, "设备已有活动任务或维护会话")
        self.maintenance_id = session_id
        self.maintenance_status = "pending"
        self.maintenance_deadline_ms = ticks_add(self._now(), int(
            timeout_s or config.MAINTENANCE_CONFIRM_TIMEOUT_S) * 1000)
        return self.maintenance_snapshot()

    def clear_maintenance(self):
        self.maintenance_id = ""
        self.maintenance_status = "none"
        self.maintenance_deadline_ms = 0

    def require_task(self, task_id, allow_paused=False, allow_pending=False):
        if self.task_status == "pending":
            if not allow_pending:
                raise DeviceError(TASK_CONFIRMATION_REQUIRED, "任务等待本地确认")
            # 只放行**属于这个待确认任务**的只读类命令（云端抓确认照片用）。
            # 这里必须自己校验 task_id：否则"放行 pending"会变成"任何 task_id 都能拍照"。
            if str(task_id or "") != (self.task_id or ""):
                raise DeviceError(TASK_NOT_ACTIVE, "命令 task_id 与待确认任务不匹配")
            return
        if self.task_status in ("pause_pending", "paused") and not allow_paused:
            raise DeviceError(TASK_PAUSED, "任务已暂停，拒绝新的运动命令")
        allowed = ("active", "pause_pending", "paused") if allow_paused else ("active",)
        if self.task_status not in allowed or not self.task_id:
            raise DeviceError(TASK_NOT_ACTIVE, "没有已确认的活动任务")
        if str(task_id or "") != self.task_id:
            raise DeviceError(TASK_NOT_ACTIVE, "命令 task_id 与活动任务不匹配")

    def require_maintenance(self):
        self.expire_local_confirmations()
        if self.maintenance_status == "pending":
            raise DeviceError(
                MAINTENANCE_CONFIRMATION_REQUIRED, "维护会话等待本地确认")
        if self.maintenance_status != "active":
            raise DeviceError(MAINTENANCE_SESSION_REQUIRED, "没有有效维护会话")

    def before_command(self, capability):
        if self.busy:
            raise DeviceError(DEVICE_BUSY, "设备正在执行 {}".format(
                self.current_capability))
        self.busy = True
        self.current_capability = capability

    def after_command(self):
        self.busy = False
        self.current_capability = ""
        if self.task_status == "pause_pending":
            return "pause_and_home"
        if self.task_status == "cancel_pending":
            return "cancel_and_home"
        return "none"

    def task_snapshot(self):
        return {
            "task_id": self.task_id,
            "status": self.task_status,
            "confirmation_deadline_ms": self.task_deadline_ms,
        }

    def maintenance_snapshot(self):
        return {
            "session_id": self.maintenance_id,
            "status": self.maintenance_status,
            "deadline_ms": self.maintenance_deadline_ms,
        }

    def snapshot(self):
        return {
            "phase": self.phase,
            "connected": self.connected,
            "connectivity": self.connectivity_snapshot(),
            "busy": self.busy,
            "current_capability": self.current_capability,
            "task": self.task_snapshot(),
            "maintenance": self.maintenance_snapshot(),
            "last_error": self.last_error,
        }
