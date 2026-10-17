"""NVS 分区里的 blob 读写：文件之外的并列副本。

NVS 是**独立分区**，`factory_reset` 擦 littlefs 上的文件时擦不到它。凡是
「文件 + NVS 各写一份」的键（如 bootstrap_key），恢复出厂必须两边都擦，
否则「文件优先、NVS 兜底」的读取路径会把旧值从 NVS 读回来 —— 表现为擦除不彻底。

固件没编 `esp32.NVS` 时全部降级为不可用，不报错。
"""


def _handle(namespace):
    """取 NVS 句柄；固件没编 esp32.NVS 时返回 None（降级，不报错）。"""
    try:
        import esp32
    except ImportError:
        return None
    try:
        return esp32.NVS(namespace)
    except Exception:
        return None


def read_blob(namespace, key):
    """读回一个 UTF-8 文本 blob；不存在或读不了返回 None。"""
    handle = _handle(namespace)
    if handle is None:
        return None
    try:
        buffer = bytearray(256)
        size = handle.get_blob(key, buffer)
        if not size:
            return None
        return bytes(buffer[:size]).decode("utf-8")
    except Exception:
        return None


def write_blob(namespace, key, value):
    """写入一个 UTF-8 文本 blob；成功返回 None，否则返回失败原因。"""
    handle = _handle(namespace)
    if handle is None:
        return "本固件没有可用的 NVS"
    try:
        handle.set_blob(key, value.encode("utf-8"))
        handle.commit()
        return None
    except Exception as error:
        return str(error)


def erase_blob(namespace, key):
    """擦掉 blob 并**复核**，绝不把"没擦掉"报成已擦。

    返回 "erased" / "missing" / "unsupported" / "failed:<原因>"。
    """
    handle = _handle(namespace)
    if handle is None:
        return "unsupported"
    if read_blob(namespace, key) is None:
        return "missing"
    try:
        handle.erase_key(key)
        handle.commit()
    except Exception as error:
        return "failed:{}".format(error)
    # 复核：擦完再读一次。读得到就是没擦掉。
    if read_blob(namespace, key) is not None:
        return "failed:key_still_present"
    return "erased"
