"""芯片身份、云端身份与注册信息持久化。"""

import config

from .compat import hexlify
from .storage import load_json, DurableRecord
from .security import random_id
from .errors import DeviceError


IDENTITY_SCHEMA_VERSION = 1


def read_hardware_id(unique_id=None):
    if unique_id is None:
        try:
            import network
        except ImportError:
            unique_id = b"desktop-test-device"
        else:
            # 与云端台账的 STA MAC 定义一致；读取失败不能悄悄回退为另一编号。
            unique_id = network.WLAN(network.STA_IF).config("mac")
            if not isinstance(unique_id, (bytes, bytearray)) or len(unique_id) != 6:
                raise DeviceError("HARDWARE_ID_UNAVAILABLE", "无法读取 STA MAC")
    raw = bytes(unique_id)
    return "esp32s3-" + hexlify(raw).lower()


class DeviceIdentity:
    def __init__(self, path=None, unique_id=None):
        self.path = path or config.IDENTITY_FILE
        self.hardware_id = read_hardware_id(unique_id)
        self.device_id = ""
        self.device_secret = ""
        self.registered_at_ms = 0
        self.registration_state = "new"
        self.transaction_id = ""
        self.record = DurableRecord(self.path + ".credentials")
        self.load()

    @property
    def registered(self):
        return bool(self.device_id and self.device_secret and
                    self.registration_state == "active" and not self.record.failed)

    def load(self):
        payload = self.record.value
        if self.record.failed:
            self.registration_state = "recovery_required"
            return False
        if payload is None:
            # 旧凭据只读导入；损坏文件不是新设备，不重新注册。
            import os
            try:
                os.stat(self.path)
                exists = True
            except OSError as error:
                exists = bool(error.args and error.args[0] != 2)
            payload = load_json(self.path, {}) or {}
            if exists and not payload:
                self.registration_state = "recovery_required"
                return False
        if not payload:
            return False
        if payload.get("schema_version") != IDENTITY_SCHEMA_VERSION:
            self.registration_state = "recovery_required"
            return False
        if payload.get("hardware_id") != self.hardware_id:
            # Flash 镜像被复制到另一块芯片时拒绝复用云端凭据。
            self.registration_state = "recovery_required"
            return False
        self.device_id = str(payload.get("device_id", ""))
        self.device_secret = str(payload.get("device_secret", ""))
        self.registered_at_ms = int(payload.get("registered_at_ms", 0) or 0)
        self.registration_state = payload.get("registration_state", "active")
        self.transaction_id = payload.get("transaction_id", "")
        return self.registered

    def _save(self, updates):
        payload = dict(self.record.value or {})
        payload.update({"schema_version": IDENTITY_SCHEMA_VERSION,
                   "hardware_id": self.hardware_id, "device_id": self.device_id,
                   "device_secret": self.device_secret,
                   "registered_at_ms": self.registered_at_ms,
                   "registration_state": self.registration_state,
                   "transaction_id": self.transaction_id})
        payload.update(updates)
        self.record.save(payload)
        self.load()

    def begin_registration(self):
        if self.registration_state == "recovery_required":
            raise DeviceError("IDENTITY_RECOVERY_REQUIRED", "身份异常，不能重新申请密钥")
        if not self.transaction_id:
            self._save({"transaction_id": random_id(), "registration_state": "requesting"})
        return self.transaction_id

    def save_pending(self, response):
        if (response.get("hardware_id") != self.hardware_id or
                response.get("registration_request_id") != self.transaction_id or
                response.get("state") != "pending" or
                not response.get("device_id") or not response.get("device_secret")):
            raise DeviceError("REGISTRATION_BINDING_MISMATCH", "领密钥响应绑定不匹配")
        self._save({"device_id": response["device_id"],
                    "device_secret": response["device_secret"],
                    "credential_version": response.get("credential_version"),
                    "registration_state": "pending"})

    def activate(self):
        self._save({"registration_state": "active"})

    def begin_reactivation(self):
        """服务端轮换 device_secret（页面上的「解除配对」）后重开一次激活事务。

        保留 device_id、绑定与 bootstrap 凭据，只把注册事务重开：下一次注册会
        重新 /challenge → /register → /confirm 换回新 secret。
        这不是"恢复出厂"——本地数据一条都不删。
        """
        if self.record.failed:
            raise DeviceError("STORAGE_RECOVERY_REQUIRED",
                              "身份记录损坏，不自动重开激活事务")
        if not self.device_id:
            raise DeviceError("REACTIVATION_NOT_AVAILABLE",
                              "本地没有可复用的绑定，无法重开激活事务")
        # 换新事务号：拿旧 transaction_id 重放注册会被服务端判 TRANSACTION_CONFLICT。
        self._save({"registration_state": "requesting", "transaction_id": random_id(),
                    "registration_payload": None, "confirm_started": False,
                    "confirm_before_ms": 0, "mqtt_ready": False})

    def prepare_recovery(self):
        """研发显式触发；不是云端授权。服务器仍必须验证恢复窗口。"""
        if not config.BOOTSTRAP_RECOVERY_CONFIRMED:
            raise DeviceError("IDENTITY_RECOVERY_REQUIRED", "恢复契约未冻结")
        if self.record.failed or not self.device_id:
            raise DeviceError("STORAGE_RECOVERY_REQUIRED", "身份损坏/缺失须研发核对，禁止冒充新设备")
        record = self.record.value or {}
        if record.get("hardware_id") != self.hardware_id:
            raise DeviceError("REGISTRATION_BINDING_MISMATCH", "硬件身份不匹配")
        if record.get("registration_purpose") == "recovery" and self.registration_state in ("requesting", "pending"):
            return self.transaction_id
        self._save({"transaction_id": random_id(), "registration_purpose": "recovery",
                    "registration_state": "requesting", "registration_payload": None,
                    "mqtt_ready": False, "confirm_started": False})
        return self.transaction_id

    def registration_payload(self, capabilities_digest=""):
        return {
            "protocol_version": config.PROTOCOL_VERSION,
            "hardware_id": self.hardware_id,
            "hardware_model": config.HARDWARE_MODEL,
            "firmware_version": config.FIRMWARE_VERSION,
            "coordinate_system_version": config.COORDINATE_SYSTEM_VERSION,
            "capabilities_digest": capabilities_digest,
        }

    def apply_registration(self, response, now_ms=0):
        device_id = str((response or {}).get("device_id", "")).strip()
        secret = str((response or {}).get("device_secret", "")).strip()
        if not device_id or not secret:
            raise ValueError("注册响应缺少 device_id 或 device_secret")
        # 已有受控凭据导入/测试入口；生产注册不得直接调用此方法。
        self._save({"device_id": device_id, "device_secret": secret,
                    "registered_at_ms": int(now_ms or 0), "registration_state": "active"})

    def public_dict(self):
        return {
            "hardware_id": self.hardware_id,
            "device_id": self.device_id,
            "registered": self.registered,
            "registration_state": self.registration_state,
            "hardware_model": config.HARDWARE_MODEL,
            "firmware_version": config.FIRMWARE_VERSION,
            "protocol_version": config.PROTOCOL_VERSION,
        }
