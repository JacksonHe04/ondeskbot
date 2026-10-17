"""凭据字段判定与输出屏蔽。

日志（`diag`）与持久化 / 接口输出（`storage`）必须用**同一套**关键词判定，
否则同一条数据在两处会得到不同的屏蔽结果。这里是那套判定的唯一来源。
"""

# 字段名包含其中任一子串即视为凭据。
SENSITIVE_WORDS = ("password", "secret", "token", "api_key", "bootstrap", "proof",
                   "auth_hex", "authorization", "factory", "credential")

MASK = "***"


def is_sensitive(name):
    """字段名是否命中凭据关键词。"""
    lowered = str(name).lower()
    return any(word in lowered for word in SENSITIVE_WORDS)


def redact_mapping(value):
    """递归屏蔽 dict / list 里的凭据字段；标量原样返回。"""
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        result[key] = MASK if is_sensitive(key) else redact_mapping(item)
    return result
