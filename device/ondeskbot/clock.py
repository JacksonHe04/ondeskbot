"""TLS、命令过期时间与 Trace 所需的可信设备时间。"""

import config

from .compat import epoch_ms
from .log import log


def clock_is_valid():
    try:
        return epoch_ms() >= int(config.TIME_VALID_AFTER_EPOCH_S) * 1000
    except Exception:
        return False


def sync_clock():
    if clock_is_valid():
        return True
    try:
        import ntptime
        if getattr(config, "NTP_HOST", ""):
            ntptime.host = config.NTP_HOST
        ntptime.settime()
    except Exception as error:
        log("[时间] NTP 同步失败: {}".format(error))
        return False
    return clock_is_valid()
