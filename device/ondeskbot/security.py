"""无外部依赖的规范编码、摘要及随机标识；不是生产注册签名规范。"""
import os
try:
    import hashlib
except ImportError:
    import uhashlib as hashlib
from .compat import json, hexlify


def canonical(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + canonical(value[k])
                              for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    return json.dumps(value)


def digest(value):
    return hexlify(hashlib.sha256(canonical(value).encode()).digest())


def random_id():
    # 随机源不可用时失败，不以芯片编号或时间代替秘密/事务随机数。
    return hexlify(os.urandom(16))


def mock_proof(key, payload):
    """仅 mock-bootstrap-v1 使用的 HMAC-SHA256。"""
    key = key.encode() if isinstance(key, str) else key
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key += b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + canonical(payload).encode()).digest()
    return hexlify(hashlib.sha256(bytes(b ^ 0x5c for b in key) + inner).digest())


def registration_proof(key_hex, nonce, hardware_id, transaction_id,
                       purpose="register"):
    """生产注册 v1；输入是 64 位小写 hex，消息不是 JSON。"""
    if (not isinstance(key_hex, str) or len(key_hex) != 64 or
            any(c not in "0123456789abcdef" for c in key_hex)):
        raise ValueError("bootstrap key must be 64 lowercase hex characters")
    fields = (nonce, hardware_id, transaction_id, "ondeskbot", purpose)
    if purpose not in ("register", "recovery") or any(
            not isinstance(v, str) or not v or "|" in v for v in fields):
        raise ValueError("invalid registration signing fields")
    key = bytes(int(key_hex[i:i+2], 16) for i in range(0, 64, 2)) + b"\x00" * 32
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) +
                           "|".join(fields).encode("utf-8")).digest()
    return hexlify(hashlib.sha256(bytes(b ^ 0x5c for b in key) + inner).digest())
