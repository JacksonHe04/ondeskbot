"""小型 JSON 持久化与原子替换。"""

import os

from .compat import json


def ensure_dir(path):
    if not path or path == "/":
        return
    parts = path.split("/")
    current = "/" if path.startswith("/") else ""
    for part in parts:
        if not part:
            continue
        if current in ("", "/"):
            current = current + part
        else:
            current = current + "/" + part
        try:
            os.mkdir(current)
        except OSError:
            pass


def parent_dir(path):
    index = path.rfind("/")
    if index <= 0:
        return "/" if path.startswith("/") else ""
    return path[:index]


def load_json(path, default=None):
    try:
        with open(path, "r") as handle:
            return json.loads(handle.read())
    except (OSError, ValueError, TypeError):
        return default


def save_json_atomic(path, value):
    ensure_dir(parent_dir(path))
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(json.dumps(value))
        handle.flush()
        if hasattr(os, "fsync"):
            os.fsync(handle.fileno())
    if hasattr(os, "sync"):
        os.sync()
    # 不先删除正式文件；不支持覆盖 rename 的文件系统必须报告失败。
    os.rename(temporary, path)
    if hasattr(os, "sync"):
        os.sync()


def remove_if_exists(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


class DurableRecord:
    """双槽代次+读回验证。任一槽损坏即锁定，不能回退后重复执行动作。

    校验和仅检测损坏，不加密。文件系统/Flash 掉电保证仍须真机验收。
    """
    def __init__(self, path):
        self.path = path
        self.generation = 0
        self.value = None
        self.failed = False
        from .security import digest
        for slot in (0, 1):
            name = path + "." + str(slot)
            try:
                os.stat(name)
            except OSError as error:
                if error.args and error.args[0] == 2:
                    continue
                self.failed = True
                continue
            try:
                with open(name) as handle:
                    record = json.loads(handle.read())
                body = record["body"]
                if digest(body) != record["checksum"]:
                    raise ValueError("checksum")
                if body["generation"] > self.generation:
                    self.generation = body["generation"]
                    self.value = body["value"]
            except Exception:
                self.failed = True

    def save(self, value):
        from .security import canonical, digest
        from .errors import DeviceError
        if self.failed:
            raise DeviceError("STORAGE_RECOVERY_REQUIRED", "持久化记录损坏或写入失败，需要核对恢复")
        body = {"generation": self.generation + 1, "value": value}
        record = {"body": body, "checksum": digest(body)}
        name = self.path + "." + str(body["generation"] % 2)
        text = canonical(record)
        try:
            ensure_dir(parent_dir(name))
            with open(name, "w") as handle:
                handle.write(text)
                handle.flush()
                if hasattr(os, "fsync"):
                    os.fsync(handle.fileno())
            if hasattr(os, "sync"):
                os.sync()
            # 读回比对**写入的原文**（逐字节），不要把解析后的对象拿来比。
            # 旧实现 `json.loads(read) != record` 会被"类型往返"误伤：写入的值里
            # 只要混进一个 tuple，JSON 往返就变成 list，比对必然不等 → 误判成写失败
            # → 记录被永久标记 failed，之后所有副作用命令全部被拒。
            # 一条脏数据不该让整台设备瘫痪；比字节既更严（真的落了原文），又不会误伤。
            with open(name) as handle:
                if handle.read() != text:
                    raise OSError("readback")
        except Exception:
            self.failed = True
            raise DeviceError("STORAGE_WRITE_FAILED", "保存或读回失败，禁止新的副作用操作")
        self.value = value
        self.generation = body["generation"]
