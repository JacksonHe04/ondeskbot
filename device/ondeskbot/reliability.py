"""有界命令日志与归档发送箱。未知动作只核对，不自动重做。"""
import config
from .storage import DurableRecord
from .security import random_id
from .errors import DeviceError


class CommandJournal:
    def __init__(self, path, limit=None):
        self.store = DurableRecord(path)
        self.limit = limit or config.COMMAND_JOURNAL_SIZE
        self.items = self.store.value or {}
        self.uncertain = any(v["state"] == "started" for v in self.items.values())

    def get(self, request_id):
        if self.store.failed:
            raise DeviceError("STORAGE_RECOVERY_REQUIRED", "命令日志损坏，禁止执行")
        return self.items.get(request_id)

    def _prune_expired(self, now_ms):
        """丢掉**已过执行期限**的条目。

        过期就丢，`final` 与 `started` 一视同仁：
        - `final` 且过期：客户端已不会再查它（`command.result.get` 也受同一期限约束）；
        - **`started` 且过期**：命令过期后会被 `command_processor` 直接拒
          （`COMMAND_EXPIRED`），**不可能被重放**，所以它不再构成"未确定动作"。

        ⚠️ 这一步**必须在 `uncertain` 检查之前**执行。旧实现把 `uncertain` 检查放在
        第一句，于是"飞行中被复位打断的 started 条目"会让 `uncertain` 永久为真：
        `start()` 每次都在第一句抛 `RESULT_UNKNOWN`，**永远走不到淘汰逻辑**，
        设备从此拒绝一切带副作用的命令，只能恢复出厂才能解开。
        真机踩到过：一条标定命令在飞行中被复位打断，之后维护与调参命令全部被
        `RESULT_UNKNOWN` 拒死。
        """
        # `.get(..., 0)`：兼容可能缺该字段的旧条目 —— 没有期限的条目
        # 也无法参与"是否可重放"的判断，按已过期处理（丢掉）。
        return {k: v for k, v in self.items.items()
                if v.get("expires_at_ms", 0) > now_ms}

    def start(self, command, fingerprint, now_ms):
        # 先自愈再判断：把已过期（含被复位打断的 started）清掉，
        # 剩下的才是真正"还没确定结局、且仍可能被重放"的动作。
        pruned = self._prune_expired(now_ms)
        if len(pruned) != len(self.items):
            self.store.save(pruned)
            self.items = pruned
        self.uncertain = any(v["state"] == "started" for v in self.items.values())
        if self.uncertain:
            raise DeviceError("RESULT_UNKNOWN", "上次启动有未确定的动作，须人工核对，禁止自动重放")
        if len(self.items) >= self.limit:
            raise DeviceError("COMMAND_JOURNAL_FULL", "命令日志已满，停止接受副作用命令")
        items = dict(self.items)
        items[command["request_id"]] = {"state": "started", "fingerprint": fingerprint,
                                      "expires_at_ms": command["expires_at_ms"]}
        self.store.save(items)
        self.items = items

    def finish(self, request_id, response):
        items = dict(self.items)
        item = dict(items[request_id])
        item.update(state="final", response=response)
        items[request_id] = item
        self.store.save(items)
        self.items = items


class Outbox:
    def __init__(self, path, limit=None):
        self.store = DurableRecord(path)
        self.limit = limit or config.OUTBOX_SIZE
        self.items = self.store.value or []

    def require_space(self, reserve=1):
        if self.store.failed or len(self.items) + reserve > self.limit:
            raise DeviceError("OUTBOX_UNAVAILABLE", "待归档发送箱已满或损坏，停止新的副作用命令")

    def add(self, kind, message):
        from .security import canonical
        message = dict(message)
        identifier = message.get("delivery_id") or random_id()
        message["delivery_id"] = identifier
        if kind == "events":
            message.setdefault("event_id", identifier)
        if any(item["id"] == identifier for item in self.items):
            return identifier
        self.require_space()
        if len(canonical(message).encode()) > config.MQTT_MAX_MESSAGE_BYTES:
            raise DeviceError("RESPONSE_TOO_LARGE", "结果超出持久发送箱单条大小上限")
        items = self.items + [{"id": identifier, "kind": kind, "message": message}]
        self.store.save(items)
        self.items = items
        return identifier

    def ack(self, identifier):
        items = [v for v in self.items if v["id"] != identifier]
        if len(items) != len(self.items):
            self.store.save(items)
            self.items = items
