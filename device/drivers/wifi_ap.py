"""配网热点（AP）：设备首次联网或重新配网时对外开的热点。

已知限制（ESP32-S3 + MicroPython）：
  - 默认 txpower=20dBm 时 RF 信号失真、beacon 不可见 → 需限制 txpower
  - STA+AP 共存时信道被锁定 → 只做纯 AP 模式

热点口令来自 `config.WIFI_AP_PASSWORD`，源码里不留真实口令。
"""

import time

import network

import config
from ondeskbot.compat import hexlify
from ondeskbot.log import log

from .wifi_manager import get_device_id, sta_if

_ap = None


def ap_if():
    """AP 接口单例。"""
    global _ap
    if _ap is None:
        _ap = network.WLAN(network.AP_IF)
    return _ap


def get_ap_ssid():
    """生成带完整 MAC 后缀的 AP SSID，例如 OnDeskBot-14c19f551814。

    该后缀与 hardware_id 的后半段逐字一致（hardware_id = "esp32s3-" + MAC）。
    """
    return config.WIFI_AP_SSID_PREFIX + get_device_id()


def start_ap():
    """启动纯 AP 配网热点；默认使用 config 中的 WPA2 共享口令。"""
    sta = sta_if()
    try:
        sta.disconnect()
    except Exception:
        pass
    sta.active(False)
    time.sleep(1)
    log("[无线网络] 无线客户端已关闭, 切换纯热点模式")

    ap = ap_if()
    ap.active(True)
    time.sleep(2)

    ap_ssid = get_ap_ssid()
    channel = config.WIFI_AP_CHANNEL
    password = getattr(config, "WIFI_AP_PASSWORD", "")
    if password:
        if len(password) < 8:
            raise ValueError("配网热点 WPA2 密码至少需要 8 位")
        ap.config(essid=ap_ssid, authmode=getattr(network, "AUTH_WPA2_PSK", 3),
                  password=password, channel=channel)
    else:
        ap.config(essid=ap_ssid, authmode=0, channel=channel)
    time.sleep(1)
    try:
        ap.config(txpower=config.WIFI_TX_POWER)
    except Exception:
        pass
    time.sleep(1)
    log("[无线网络] 热点配置: 名称={} 安全={} 信道={} 发射功率={}".format(
        ap_ssid, "WPA2" if password else "开放", channel, config.WIFI_TX_POWER))

    # 启动后回读一次真实配置：这个固件会静默改写信道与发射功率。
    try:
        log("[无线网络] 热点诊断: MAC={} 信道={} 发射功率={} 已启用={}".format(
            hexlify(ap.config('mac'), ':'), ap.config('channel'),
            ap.config('txpower'), ap.active()))
    except Exception as e:
        log("[无线网络] 热点诊断失败:", e)

    try:
        ap.ifconfig((config.WIFI_AP_IP, config.WIFI_AP_SUBNET,
                     config.WIFI_AP_IP, config.WIFI_AP_IP))
    except Exception as e:
        log("[无线网络] 自定义热点 IP 失败: {}, 使用默认 IP".format(e))

    log("[无线网络] 热点已启动: {} @ {}".format(ap_ssid, ap.ifconfig()[0]))
    return True


def get_ap_ip():
    """获取 AP 实际 IP 地址"""
    try:
        return ap_if().ifconfig()[0]
    except Exception:
        return config.WIFI_AP_IP


def stop_ap():
    """停止 AP 热点"""
    ap_if().active(False)
    log("[无线网络] 热点已关闭")
