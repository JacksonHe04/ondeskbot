"""先校验归属/去重，再检查期限、权限、忙碌；没有动作执行队列。"""
from .errors import DeviceError, INVALID_REQUEST
from .protocol import ResponseCache, response_envelope, validate_command
from .security import digest, random_id
from .compat import epoch_ms
from .clock import clock_is_valid


class CommandProcessor:
    def __init__(self, identity, registry, runtime, safety_transition=None,
                 now_provider=None, cache=None, journal=None, admission_guard=None,
                 clock_valid=None, strict=False):
        self.identity, self.registry, self.runtime = identity, registry, runtime
        self.safety_transition = safety_transition or (lambda transition: None)
        self.now_provider = now_provider or epoch_ms
        self.cache = cache or ResponseCache()
        self.journal = journal
        self.admission_guard = admission_guard or (lambda: None)
        self.clock_valid = clock_valid or clock_is_valid
        self.strict = strict
        self.inflight = {}

    def _response(self, command, ok, result=None, error=None):
        result = response_envelope(self.identity, command, ok, result, error,
                                   now_ms=self.now_provider())
        result["delivery_id"] = random_id()
        return result

    def _cached_copy(self, response):
        copy = dict(response)
        copy["payload"] = dict(response["payload"])
        copy["payload"]["cached"] = True
        return copy

    def lookup(self, request_id):
        item = self.cache.get(request_id)
        if item:
            return {"state": "final", "response": item["response"]}
        if request_id in self.inflight:
            return {"state": "in_progress"}
        item = self.journal.get(request_id) if self.journal else None
        if item:
            return {"state": "final", "response": item["response"]} if item["state"] == "final" else {"state": "unknown"}
        return {"state": "unknown", "reason": "not_retained_or_never_received"}

    def handle(self, message, forced_busy=False):
        command = {"request_id": "", "task_id": "", "capability": ""}
        started = False
        journaled = False
        fingerprint = None
        cacheable = False
        owns_inflight = False
        try:
            command = validate_command(message, device_id=self.identity.device_id, strict=self.strict)
            fingerprint = digest(command)
            cached = self.cache.get(command["request_id"])
            stored = self.journal.get(command["request_id"]) if self.journal else None
            prior = cached or stored
            active = self.inflight.get(command["request_id"])
            if (prior and prior["fingerprint"] != fingerprint) or (active and active != fingerprint):
                raise DeviceError("REQUEST_ID_CONFLICT", "同一 request_id 的内容不同")
            if cached or (stored and stored["state"] == "final"):
                return self._cached_copy(prior["response"])
            if active:
                return self._response(command, True, {"state": "in_progress"})
            if stored:
                raise DeviceError("RESULT_UNKNOWN", "上次启动未记录最终结果，禁止重新执行")
            cacheable = True
            spec = self.registry.get(command["capability"])
            timed = spec.mutating or spec.motion or spec.hardware
            expires = command["expires_at_ms"]
            if timed and not expires:
                raise DeviceError(INVALID_REQUEST, "硬件/状态变更命令必须有 expires_at_ms")
            if (timed or expires) and not self.clock_valid():
                raise DeviceError("TIME_SYNC_REQUIRED", "设备时间不可信，拒绝时效命令")
            if expires and self.now_provider() >= expires:
                raise DeviceError("COMMAND_EXPIRED", "命令已过开始期限")
            self.registry.authorize(spec, command, self.runtime)
            concurrent = spec.concurrent or spec.safety
            if (forced_busy or self.runtime.busy) and not concurrent:
                raise DeviceError("DEVICE_BUSY", "设备忙碌；此命令不会排队")
            durable = timed and not spec.safety
            if durable:
                self.admission_guard()
                if self.journal:
                    self.journal.start(command, fingerprint, self.now_provider())
                    journaled = True
            self.inflight[command["request_id"]] = fingerprint
            owns_inflight = True
            if not concurrent:
                self.runtime.before_command(spec.name)
                started = True
            if expires and self.now_provider() >= expires:
                raise DeviceError("COMMAND_EXPIRED", "开始执行前命令已过期")
            result = spec.handler(command["args"], command) if spec.with_command else spec.handler(command["args"])
            if started:
                from .cooperative import checkpoint
                checkpoint()
            response = self._response(command, True, result)
        except DeviceError as error:
            self.runtime.last_error = error.as_dict()
            response = self._response(command, False, error=error.as_dict())
        except Exception as error:
            wrapped = DeviceError("INTERNAL_ERROR", "设备内部错误", {"type": error.__class__.__name__})
            self.runtime.last_error = wrapped.as_dict()
            response = self._response(command, False, error=wrapped.as_dict())
        finally:
            if started:
                transition = "pause_and_home" if self.runtime.task_status == "pause_pending" else (
                    "cancel_and_home" if self.runtime.task_status == "cancel_pending" else "none")
                if transition != "none":
                    try:
                        self.safety_transition(transition)
                    except Exception:
                        self.runtime.last_error = {"code": "SAFETY_HOME_FAILED", "message": "安全归零失败"}
                self.runtime.busy = False
                self.runtime.current_capability = ""
        if journaled:
            try:
                self.journal.finish(command["request_id"], response)
            except DeviceError as error:
                self.journal.uncertain = True
                response = self._response(command, False, error=error.as_dict())
                cacheable = False
        if owns_inflight:
            self.inflight.pop(command.get("request_id"), None)
        if command.get("request_id") and cacheable:
            self.cache.put(command["request_id"], {"fingerprint": fingerprint, "response": response})
        return response
