"""云端协议草案的消息校验与响应封装。"""

import config

from .compat import epoch_ms
from .errors import DeviceError, INVALID_REQUEST


def validate_command(message, now_ms=None, device_id=None, strict=False):
    if not isinstance(message, dict):
        raise DeviceError(INVALID_REQUEST, "命令必须是 JSON 对象")
    if strict and (message.get("protocol_version") != config.PROTOCOL_VERSION or
                   message.get("device_id") != device_id or
                   message.get("message_type") != "command"):
        raise DeviceError(INVALID_REQUEST, "协议版本、消息类型或设备归属不匹配")
    if device_id and message.get("device_id", device_id) != device_id:
        raise DeviceError(INVALID_REQUEST, "命令不属于本设备")
    if message.get("message_type") not in (None, "command"):
        raise DeviceError(INVALID_REQUEST, "message_type 必须为 command")
    payload = message.get("payload", message)
    if not isinstance(payload, dict):
        raise DeviceError(INVALID_REQUEST, "payload 必须是对象")

    request_id = payload.get("request_id", "")
    capability = payload.get("capability", "")
    if not isinstance(request_id, str) or not isinstance(capability, str):
        raise DeviceError(INVALID_REQUEST, "request_id/capability 必须为字符串")
    if not request_id or len(request_id) > 64:
        raise DeviceError(INVALID_REQUEST, "request_id 缺失或过长")
    if not capability or len(capability) > 96:
        raise DeviceError(INVALID_REQUEST, "capability 缺失或过长")
    if request_id != request_id.strip() or capability != capability.strip():
        raise DeviceError(INVALID_REQUEST, "标识符不能带首尾空白")
    for key in ("task_id", "auth_scope"):
        if key in payload and (not isinstance(payload[key], str) or len(payload[key]) > 128):
            raise DeviceError(INVALID_REQUEST, key + " 格式错误")
    args = payload.get("args", {})
    if not isinstance(args, dict):
        raise DeviceError(INVALID_REQUEST, "args 必须是对象")

    expires_at_ms = payload.get("expires_at_ms", 0)
    issued = payload.get("issued_at_ms", 0)
    if (type(expires_at_ms) is not int or type(issued) is not int or
            expires_at_ms < 0 or issued < 0):
        raise DeviceError(INVALID_REQUEST, "时间必须是非负整数 Unix 毫秒")

    return {
        "request_id": request_id,
        "task_id": str(payload.get("task_id", "")).strip(),
        "capability": capability,
        "args": args,
        "issued_at_ms": issued,
        "expires_at_ms": expires_at_ms,
        "auth_scope": str(payload.get("auth_scope", "agent") or "agent"),
    }


def response_envelope(identity, command, ok, result=None, error=None,
                      now_ms=None, cached=False):
    return {
        "protocol_version": config.PROTOCOL_VERSION,
        "message_type": "command_response",
        "device_id": identity.device_id,
        "hardware_id": identity.hardware_id,
        "timestamp_ms": int(now_ms if now_ms is not None else epoch_ms()),
        "payload": {
            "request_id": command.get("request_id", ""),
            "task_id": command.get("task_id", ""),
            "capability": command.get("capability", ""),
            "ok": bool(ok),
            "cached": bool(cached),
            "result": result if ok else None,
            "error": error if not ok else None,
        },
    }


def event_envelope(identity, event_name, data=None, now_ms=None):
    return {
        "protocol_version": config.PROTOCOL_VERSION,
        "message_type": "event",
        "device_id": identity.device_id,
        "hardware_id": identity.hardware_id,
        "timestamp_ms": int(now_ms if now_ms is not None else epoch_ms()),
        "payload": {"event": event_name, "data": data or {}},
    }


class ResponseCache:
    """小型内存幂等缓存；重复 request_id 不会再次执行机械动作。"""
    def __init__(self, limit=None):
        self.limit = int(limit or config.RESPONSE_CACHE_SIZE)
        self._order = []
        self._items = {}

    def get(self, request_id):
        return self._items.get(request_id)

    def put(self, request_id, response):
        if request_id in self._items:
            self._items[request_id] = response
            return
        self._items[request_id] = response
        self._order.append(request_id)
        while len(self._order) > self.limit:
            oldest = self._order.pop(0)
            self._items.pop(oldest, None)
