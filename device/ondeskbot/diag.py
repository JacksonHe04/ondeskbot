"""串口诊断行：打印云端链路的状态迁移，绝不打印凭据。

联调验收要求"每条结论都有串口原文"，而云端层此前一行都不打（只有驱动会打）。
这里只打**状态与判据**：hardware_id、device_id、注册状态、CONNACK 码、订阅结果、
错误码。字段名命中凭据关键词时一律打 `***`，判定与屏蔽共用 `redact` 模块。
"""

from .log import log
from .redact import MASK, is_sensitive

ENABLED = True


def _format(value):
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # 单行约束：串口证据必须一行一条，便于两边对照。
    return text.replace("\n", "\\n").replace(" ", "_")


def note(event, **fields):
    if not ENABLED:
        return
    parts = ["[云]", str(event)]
    for name in sorted(fields):
        value = fields[name]
        parts.append("{}={}".format(name, MASK if is_sensitive(name) else _format(value)))
    log(" ".join(parts))


def error(error, context=""):
    """一条 DeviceError 的诊断行；details 里只保留非凭据标量。"""
    note("error", where=context, code=error.code, retryable=error.retryable,
         message=str(error.message)[:80])
