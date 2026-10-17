"""ESP-IDF 原生 WiFi 状态码 → 可读说明。

本固件的 `WLAN.status()` 返回的是 ESP-IDF 的 `wifi_err_reason_t`，不是 MicroPython
的 `STAT_*` 抽象码（例如 `STAT_WRONG_PASSWORD` 在本固件是 202，实际不会出现），
因此不能直接套用 MicroPython 文档里的对照表。
"""

_AUTH_NAMES = {
    0: "开放", 1: "WEP", 2: "WPA", 3: "WPA2",
    4: "WPA/WPA2", 5: "WPA2企业", 6: "WPA3", 7: "WPA2/WPA3",
}

_STATUS_NAMES = {
    0: "空闲", 1: "连接中", 2: "认证超时", 3: "认证离开", 4: "关联超时",
    6: "未认证", 7: "未关联", 8: "关联离开", 9: "关联未认证",
    14: "密码错误(MIC 校验失败)", 15: "四次握手超时(多为密码错误)",
    16: "组密钥更新超时", 23: "802.1X 认证失败", 24: "加密套件被拒",
    38: "AP 安全性与设备不兼容", 39: "AP 认证方式不达阈值", 40: "AP 信号低于阈值",
    200: "信标超时", 201: "未找到热点", 202: "认证失败",
    203: "关联失败", 204: "握手超时", 205: "连接失败",
    1000: "空闲", 1001: "连接中", 1010: "已获取IP",
}

_FAIL_CODES = (2, 3, 4, 6, 14, 15, 16, 23, 24, 38, 39, 40,
               200, 201, 202, 203, 204, 205)
# 只有这些才算「密码确实不对」。2 曾在此列表里，但它是 AUTH_EXPIRE —— 认证超时是
# 瞬时故障，重试往往就能连上（真机实测：同一密码第 5 次成功）。把它当作致命错误
# 会直接放弃重试，表现为「设备怎么也连不上、而手动重试却能连」。
_WRONG_PW_CODES = (14, 15, 202)


def auth_name(code):
    """认证方式的显示名。"""
    return _AUTH_NAMES.get(code, "认证={}".format(code))


def status_name(code):
    """连接状态的显示名；未知码原样返回数字。"""
    return _STATUS_NAMES.get(code, str(code))


def is_failure(code):
    """该状态码是否代表一次失败（可据此提前结束等待，不必等满超时）。"""
    return code in _FAIL_CODES


def is_wrong_password(code):
    """该状态码是否确凿指向密码错误（此时重试无意义）。"""
    return code in _WRONG_PW_CODES
