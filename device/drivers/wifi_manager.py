"""WiFi STA：凭据存储、连接、非阻塞重连与状态查询。

凭据以 JSON 落在 flash（路径见 `config.WIFI_CRED_FILE`），源码里不留任何真实
SSID / 口令。配网热点（AP）的部分见 `wifi_ap.py`。
"""

import json
import os
import time

import network

import config
from ondeskbot.compat import hexlify
from ondeskbot.log import log

from . import wifi_status

_sta = None
_saved_reconnect_started_ms = 0
_saved_reconnect_last_status = -1


def sta_if():
    """STA 接口单例。"""
    global _sta
    if _sta is None:
        _sta = network.WLAN(network.STA_IF)
    return _sta


def get_device_id():
    """取完整 STA MAC（12 位小写十六进制）作为设备标识后缀。

    产线需要在出厂前读取 hardware_id、去服务端与 bootstrap_key 绑定。把完整
    MAC 放进热点名后，扫一次 Wi-Fi 列表即可读到所有在配网状态的设备编号，不必
    逐台连接；MAC 是公开编号、不是认证凭据，因此可以广播。
    """
    return hexlify(sta_if().config('mac'))


def save_credentials(ssid, password):
    """保存 WiFi 凭据到 flash 文件系统"""
    data = {"ssid": ssid, "password": password}
    try:
        temp_path = config.WIFI_CRED_FILE + ".tmp"
        with open(temp_path, "w") as f:
            json.dump(data, f)
        # 先写临时文件，再原子替换。不要先删除旧文件——那会在 remove 与 rename
        # 之间留下「凭据文件不存在」的窗口，此时复位或掉电会永久丢失 Wi-Fi 凭据，
        # 设备下次启动会直接进入配网热点模式。
        try:
            os.rename(temp_path, config.WIFI_CRED_FILE)
        except OSError:
            os.remove(config.WIFI_CRED_FILE)
            os.rename(temp_path, config.WIFI_CRED_FILE)
        log("[无线网络] 凭据已保存")
    except Exception as e:
        log("[无线网络] 保存凭据失败:", e)


def load_credentials():
    """从 flash 读取 WiFi 凭据，返回 (ssid, password) 或 (None, None)"""
    try:
        with open(config.WIFI_CRED_FILE, "r") as f:
            data = json.load(f)
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        if ssid:
            return ssid, password
    except Exception:
        pass
    return None, None


def clear_credentials():
    """清除已保存的 WiFi 凭据。"""
    try:
        os.remove(config.WIFI_CRED_FILE)
        log("[无线网络] 已清除保存的凭据")
        return True
    except Exception:
        return False


def _pw_hint(password):
    """日志里的口令摘要：只留前两位与长度，绝不落明文。"""
    return "{}***({})".format(password[:2], len(password)) if password else "(空)"


def connect(ssid, password):
    """
    连接指定 WiFi，阻塞等待直到连接成功或超时。
    成功返回 True 并保存凭据。
    """
    if not ssid:
        log("[无线网络] 无线网络名称为空，跳过连接")
        return False

    log("[无线网络] 连接参数: 名称='{}' 密码={}".format(ssid, _pw_hint(password)))

    sta = sta_if()
    sta.active(True)
    time.sleep(1)

    try:
        sta.config(txpower=config.WIFI_TX_POWER)
    except Exception:
        pass

    try:
        raw = sta.scan()
        found = False
        log("[无线网络] 扫描到 {} 个网络:".format(len(raw)))
        for item in raw:
            name = item[0].decode("utf-8", "ignore")
            marker = " <<<" if name == ssid else ""
            found = found or name == ssid
            log("  {} (信道={} 信号={} {}){}".format(
                name, item[2], item[3], wifi_status.auth_name(item[4]), marker))
        if not found:
            log("[无线网络] 警告: 目标 '{}' 未在扫描中出现!".format(ssid))
            log("[无线网络] 可能原因: 1)5GHz网络 2)信号太弱 3)名称隐藏")
    except Exception as e:
        log("[无线网络] 扫描异常:", e)

    try:
        sta.config(reconnects=0)
    except Exception:
        pass

    for attempt in range(config.WIFI_STA_MAX_RETRIES):
        log("[无线网络] 连接 {} (第 {}/{} 次)...".format(
            ssid, attempt + 1, config.WIFI_STA_MAX_RETRIES))

        try:
            sta.disconnect()
        except Exception:
            pass
        time.sleep(0.5)

        sta.connect(ssid, password)

        deadline = time.time() + config.WIFI_STA_TIMEOUT_S
        grace_until = time.time() + 5
        last_st = -1
        connected = False
        early_fail = False

        while time.time() < deadline:
            if sta.isconnected():
                connected = True
                break

            st = sta.status()
            if st != last_st:
                log("[无线网络]   状态: {} ({})".format(st, wifi_status.status_name(st)))
                last_st = st

            # 失败码要等过宽限期才认：驱动刚 connect 时常先报一个历史状态。
            if wifi_status.is_failure(st) and time.time() > grace_until:
                early_fail = True
                break

            time.sleep(1)

        if connected:
            log("[无线网络] 已连接! IP={}".format(sta.ifconfig()[0]))
            save_credentials(ssid, password)
            return True

        st = sta.status()
        sn = wifi_status.status_name(st)
        log("[无线网络] {}: {} ({})".format("连接失败" if early_fail else "连接超时", st, sn))

        if wifi_status.is_wrong_password(st):
            log("[无线网络] 密码错误, 停止重试")
            sta.disconnect()
            return False

        sta.disconnect()
        time.sleep(2)

    log("[无线网络] 连接 {} 失败".format(ssid))
    return False


def connect_saved():
    """尝试从 flash 读取凭据并连接，成功返回 True"""
    ssid, password = load_credentials()
    if ssid is None:
        log("[无线网络] 无已保存的凭据")
        return False
    log("[无线网络] 从闪存读取: 名称='{}' 密码={}".format(ssid, _pw_hint(password)))
    return connect(ssid, password)


def start_saved_reconnect():
    """非阻塞启动一次已保存 Wi-Fi 的重连，供设备主循环调用。"""
    global _saved_reconnect_started_ms, _saved_reconnect_last_status
    ssid, password = load_credentials()
    if ssid is None:
        return {"started": False, "reason": "no_saved_credentials"}

    sta = sta_if()
    if sta.isconnected():
        return {"started": False, "reason": "already_connected"}
    sta.active(True)
    try:
        sta.config(txpower=config.WIFI_TX_POWER)
        sta.config(reconnects=0)
    except Exception:
        pass
    try:
        sta.disconnect()
    except Exception:
        pass
    sta.connect(ssid, password)
    _saved_reconnect_started_ms = time.ticks_ms()
    _saved_reconnect_last_status = -1
    log("[无线网络] 已启动非阻塞重连: {}".format(ssid))
    return {"started": True, "ssid": ssid}


def poll_saved_reconnect():
    """返回 idle/connecting/connected/failed，不阻塞按键和设备主循环。"""
    global _saved_reconnect_started_ms, _saved_reconnect_last_status
    sta = sta_if()
    if sta.isconnected():
        _saved_reconnect_started_ms = 0
        return {"state": "connected", "ip": sta.ifconfig()[0]}
    if not _saved_reconnect_started_ms:
        return {"state": "idle"}

    status = sta.status()
    elapsed_ms = time.ticks_diff(time.ticks_ms(), _saved_reconnect_started_ms)
    if status != _saved_reconnect_last_status:
        log("[无线网络] 重连状态: {} ({})".format(
            status, wifi_status.status_name(status)))
        _saved_reconnect_last_status = status

    # 给关联/握手保留短暂宽限，避免驱动刚启动就报告历史失败码。
    failed = wifi_status.is_failure(status) and elapsed_ms >= 3000
    timed_out = elapsed_ms >= int(config.WIFI_RECONNECT_ATTEMPT_TIMEOUT_S) * 1000
    if failed or timed_out:
        try:
            sta.disconnect()
        except Exception:
            pass
        _saved_reconnect_started_ms = 0
        return {
            "state": "failed",
            "reason": "driver_status" if failed else "timeout",
            "status": status,
            "elapsed_ms": elapsed_ms,
        }
    return {"state": "connecting", "status": status, "elapsed_ms": elapsed_ms}


def cancel_saved_reconnect():
    global _saved_reconnect_started_ms
    _saved_reconnect_started_ms = 0
    try:
        sta_if().disconnect()
    except Exception:
        pass


def is_connected():
    """STA 是否已连接且有有效 IP"""
    sta = sta_if()
    return sta.isconnected() and sta.ifconfig()[0] != "0.0.0.0"


def get_ip():
    """获取 STA IP 地址"""
    sta = sta_if()
    return sta.ifconfig()[0] if sta.isconnected() else "0.0.0.0"


def get_ssid():
    """获取当前连接的 SSID"""
    try:
        return sta_if().config('essid')
    except Exception:
        return ""


def scan_networks():
    """扫描附近 WiFi 网络，返回 [{ssid, rssi, secure}] 列表"""
    sta = sta_if()
    if not sta.active():
        sta.active(True)

    results = []
    try:
        raw = sta.scan()
        seen = set()
        for item in raw:
            ssid = item[0].decode("utf-8", "ignore")
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            results.append({
                "ssid": ssid,
                "rssi": item[3],
                "secure": item[4] != 0,
            })
        results.sort(key=lambda x: x["rssi"], reverse=True)
    except Exception as e:
        log("[无线网络] 扫描失败:", e)

    log("[无线网络] 扫描到 {} 个网络".format(len(results)))
    return results
