"""可替换注册接口；生产签名/注入/确认契约未冻结，生产实现故意关闭。"""
import config
from .errors import DeviceError, TRANSPORT_NOT_CONFIGURED
from .http_client import HttpClient
from .security import mock_proof
from .storage import load_json


class RegistrationClient:
    def register_if_needed(self, identity, capabilities_digest):
        raise NotImplementedError


class DisabledRegistrationClient(RegistrationClient):
    def register_if_needed(self, identity, capabilities_digest):
        if identity.registered:
            return {"registered": True, "device_id": identity.device_id}
        raise DeviceError(TRANSPORT_NOT_CONFIGURED,
                          "生产注册契约未冻结或配置缺失；禁止以公开芯片编号领取密钥")


def registration_error(response):
    status = response.status
    if status == 401:
        return DeviceError("AUTHENTICATION_REJECTED", "认证凭据未被接受；保留身份并排查")
    if status == 403:
        return DeviceError("REGISTRATION_FORBIDDEN", "权限/状态/网关拒绝；禁止盲目重试")
    if status == 429:
        value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), "")
        delay = None
        try:
            delay = max(0, int(value))
        except (ValueError, TypeError):
            try:
                from .compat import epoch_ms
                delay = max(0, int(http_date_seconds(value) - epoch_ms() / 1000))
            except (ValueError, TypeError, IndexError):
                pass
        # 固件无法解析 HTTP 日期则保留原值，采用保守最大退避，不当毫秒。
        return DeviceError("RATE_LIMITED", "注册限流，使用原事务退避重试",
                           {"retry_after_s": delay if delay is not None else config.CLOUD_RECONNECT_MAX_S}, True)
    return DeviceError("REGISTRATION_FAILED", "注册接口失败",
                       {"http_status": status}, retryable=status >= 500)


def http_date_seconds(value):
    """RFC 1123 / IMF-fixdate 的 UTC 解析，MicroPython 无 email/datetime 依赖。"""
    parts = value.split()
    if len(parts) != 6 or parts[5] != "GMT":
        raise ValueError("invalid HTTP date")
    day, year = int(parts[1]), int(parts[3])
    month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(parts[2]) + 1
    hour, minute, second = [int(v) for v in parts[4].split(":")]
    leap = lambda y: y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    lengths = [31, 29 if leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1970 <= year <= 9999 and 1 <= day <= lengths[month-1] and 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError("invalid HTTP date")
    days = sum(366 if leap(y) else 365 for y in range(1970, year))
    days += sum(lengths[:month-1]) + day - 1
    return days * 86400 + hour * 3600 + minute * 60 + second


class HttpRegistrationClient(RegistrationClient):
    """仅 mock-bootstrap-v1。生产必须换适配器，不能只填写生产 URL。"""
    def __init__(self, url, http=None, allow_insecure=False, bootstrap_key=None):
        self.url = str(url or "").rstrip("/")
        self.http = http or HttpClient()
        self.allow_insecure = allow_insecure
        self.bootstrap_key = bootstrap_key

    def _post(self, suffix, data):
        response = self.http.json_request("POST", self.url + suffix, data)
        if not 200 <= response.status < 300:
            raise registration_error(response)
        body = response.json()
        if body.get("mock_protocol") != "mock-bootstrap-v1":
            raise DeviceError("REGISTRATION_CONTRACT_MISMATCH", "不是显式 Mock 注册协议")
        return body

    def register_if_needed(self, identity, capabilities_digest):
        if identity.registered and (identity.record.value or {}).get("mqtt_ready") is True:
            return {"registered": True, "device_id": identity.device_id, "reused": True}
        if not self.allow_insecure or not self.url.startswith("http://"):
            raise DeviceError(TRANSPORT_NOT_CONFIGURED, "生产注册未实现；此适配器仅允许显式 HTTP Mock")
        if identity.registration_state not in ("pending", "active"):
            identity.begin_registration()
        if identity.registration_state not in ("pending", "active"):
            key = self.bootstrap_key
            if not key:
                key = (load_json(config.MOCK_BOOTSTRAP_KEY_FILE, {}) or {}).get("bootstrap_key")
            if not key:
                raise DeviceError(TRANSPORT_NOT_CONFIGURED, "缺少开发测试注入凭据，不能领密钥")
            request = identity.registration_payload(capabilities_digest)
            request["registration_request_id"] = identity.transaction_id
            challenge = self._post("/challenge", request)
            signed = dict(request)
            signed.update(mock_protocol="mock-bootstrap-v1", purpose="first-registration",
                          challenge_id=challenge["challenge_id"], nonce=challenge["nonce"])
            pending = self._post("/claim", {"signed": signed, "proof": mock_proof(key, signed)})
            identity.save_pending(pending)
        active = self._post("/confirm", {
            "device_id": identity.device_id, "device_secret": identity.device_secret,
            "registration_request_id": identity.transaction_id})
        if (active.get("state") != "active" or active.get("device_id") != identity.device_id or
                active.get("registration_request_id") != identity.transaction_id):
            raise DeviceError("REGISTRATION_BINDING_MISMATCH", "确认响应不匹配")
        identity._save({"registration_state": "active", "mqtt_ready": active.get("mqtt_ready") is True})
        if not active.get("mqtt_ready"):
            raise DeviceError("BROKER_NOT_READY", "凭据已激活，等待 Broker 同步；不重新注册", retryable=True)
        return {"registered": True, "device_id": identity.device_id, "reused": False}
