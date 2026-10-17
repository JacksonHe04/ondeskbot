"""硬件能力注册表、元数据与统一安全校验。"""

from .errors import DeviceError, UNAUTHORIZED, UNKNOWN_CAPABILITY


class CapabilitySpec:
    def __init__(self, name, handler, category, access="agent",
                 requires_task=False, allow_pending=False, motion=False, mutating=False,
                 description="", args_schema=None, with_command=False,
                 concurrent=False, safety=False, hardware=False):
        self.name = name
        self.handler = handler
        self.category = category
        self.access = access
        self.requires_task = bool(requires_task)
        # 是否允许在任务 pending（等本地确认）期间调用：只给「抓确认照片」这类
        # 不改机械状态的命令用；默认 False，不给运动类命令开口子。
        self.allow_pending = bool(allow_pending)
        self.motion = bool(motion)
        self.mutating = bool(mutating)
        self.description = description
        from .capability_contract import parameters
        self.args_schema = parameters(name) if args_schema is None else args_schema
        self.with_command = bool(with_command)
        self.concurrent, self.safety, self.hardware = concurrent, safety, hardware

    def public_dict(self):
        return {
            "name": self.name,
            "category": self.category,
            "access": self.access,
            "requires_task": self.requires_task,
            "allow_pending": self.allow_pending,
            "motion": self.motion,
            "mutating": self.mutating,
            "concurrent": self.concurrent,
            "hardware": self.hardware,
            "safety": self.safety,
            "description": self.description,
            "args": self.args_schema,
            "contract_version": 1,
            "result": {"type": "object", "description": "command_response.payload.result；具体字段随能力而定"},
            "errors": ["INVALID_ARGUMENT", "UNAUTHORIZED", "DEVICE_BUSY", "COMMAND_EXPIRED"],
        }


class CapabilityRegistry:
    def __init__(self):
        self._items = {}

    def add(self, spec):
        if spec.name in self._items:
            raise ValueError("重复能力: {}".format(spec.name))
        self._items[spec.name] = spec
        return spec

    def register(self, name, handler, category, **kwargs):
        return self.add(CapabilitySpec(name, handler, category, **kwargs))

    def get(self, name):
        spec = self._items.get(name)
        if not spec:
            raise DeviceError(UNKNOWN_CAPABILITY, "未知能力: {}".format(name))
        return spec

    def list_public(self):
        return [self._items[name].public_dict() for name in sorted(self._items)]

    def names(self):
        return sorted(self._items)

    def digest(self):
        from .capability_contract import canonical_manifest, SCHEMA_VERSION
        from .security import hashlib
        from .compat import hexlify
        # 流式 hash：避免同时构造整份能力目录的大字符串。
        hasher = hashlib.sha256()
        hasher.update(b'{"capabilities":[')
        for index, name in enumerate(self.names()):
            if index:
                hasher.update(b',')
            hasher.update(canonical_manifest(self._items[name].public_dict()).encode("utf-8"))
        hasher.update(('],"schema_version":%d}' % SCHEMA_VERSION).encode())
        return hexlify(hasher.digest())

    def page(self, args):
        from .errors import INVALID_ARGUMENT
        from .capability_contract import SCHEMA_VERSION
        offset, limit = args.get("offset", 0), args.get("limit", 2)
        if (type(offset) is not int or type(limit) is not int or
                not 0 <= offset <= len(self._items) or not 1 <= limit <= 4):
            raise DeviceError(INVALID_ARGUMENT, "offset/limit 超出能力分页范围")
        digest = self.digest()
        if args.get("expected_digest", digest) != digest:
            raise DeviceError("CAPABILITIES_CHANGED", "能力目录已变化，请从第一页重新读取")
        names = self.names()
        end = min(offset + limit, len(names))
        return {"schema_version": SCHEMA_VERSION, "digest": digest,
                "total": len(names), "offset": offset,
                "next_offset": end if end < len(names) else None,
                "capabilities": [self._items[name].public_dict() for name in names[offset:end]]}

    def authorize(self, spec, command, runtime):
        scope = command.get("auth_scope", "agent")
        allowed_scopes = {
            "agent": ("agent", "system", "maintenance"),
            "system": ("system", "maintenance"),
            "setup": ("setup", "maintenance"),
            "maintenance_control": ("maintenance",),
            "maintenance": ("maintenance",),
        }
        if scope not in allowed_scopes.get(spec.access, (spec.access,)):
            raise DeviceError(
                UNAUTHORIZED,
                "auth_scope={} 无权调用 {} 能力".format(scope, spec.access),
            )
        if spec.access in ("maintenance", "maintenance_control"):
            if scope != "maintenance":
                raise DeviceError(UNAUTHORIZED, "能力需要 maintenance 云端权限")
        if spec.access == "maintenance":
            runtime.require_maintenance()
        if spec.requires_task:
            runtime.require_task(command.get("task_id"), allow_paused=not spec.motion,
                                 allow_pending=spec.allow_pending)
