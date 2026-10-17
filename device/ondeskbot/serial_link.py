"""串口接入协议 v1：USB-CDC 上的 identify / write_key / read_key。

协议以串口协议 v1（冻结版）为唯一权威。本模块只做搬运：
**不实现任何加解密**（协议要求 5，bootstrap key 的验证发生在设备注册时）。

三条命令：`{"cmd":"identify"}` / `{"cmd":"write_key","key":"<64hex>"}` / `{"cmd":"read_key"}`。
成功 `{"ok":true,...}`，失败 `{"ok":false,"message":"..."}`；未知 cmd 同样回失败。

## 为什么 key 要写两处

协议要求 2 写的是「写进 NVS」。但本机固件的注册路径读的是 `config.BOOTSTRAP_KEY_FILE`
指向的文件（见 `production_registration`）——只写 NVS 的话，key 会「写进去了但设备
永远用不上」：回执 ok 但注册仍不带这把 key，属于假成功。所以 `KeyStore.write()`
两处都写：文件是注册真正读的地方，NVS 是并列副本。

**读取优先级：文件优先，NVS 兜底。** 这样在「串口写」与「既有命令行写（只写文件）」
的任意先后顺序下都不会出现两把 key；反过来若 NVS 优先，命令行写完之后就会出现
「read_key 报 A、注册却用 B」的漂移。

## 为什么监听线程要主动让出串口

监听线程会抢 `sys.stdin`。若不主动退出，它会把 `mpremote` 发来的 `\r\x03`（打断）
和 `\r\x01`（进 raw REPL）吃掉，导致 `mpremote` 再也进不来 —— 而部署脚本
`deploy.sh` 依赖 `fs sha256sum` 读回校验，会整体瘫痪。因此收到任何**裸控制字符**
即视为「控制台要回去了」：立刻停止监听、不再参与竞争。
副作用：进过 REPL 之后必须断电重启才会重新开始监听。
"""

import sys

import config

from . import nvs
from .compat import json, sleep_ms, ticks_diff, ticks_ms
from .log import log
from .storage import load_json, save_json_atomic

IDENTIFY = "identify"
WRITE_KEY = "write_key"
READ_KEY = "read_key"

_HEX = "0123456789abcdef"
# 行尾：协议写的是 \n，但上位机各种写法都有（`\n` / `\r\n` / 裸 `\r`）。
# JSON 里不可能出现裸 \r（转义是 \\r），所以把裸 \r 也当行尾是纯增益 ——
# 否则对方用裸 \r 结尾时，我们会一直等不到行尾、看起来像设备没响应。
_LINE_END = (0x0a, 0x0d)
# 允许出现在 JSON 行里的控制字符；其余裸控制字符一律视为 REPL/mpremote 在抢串口。
_INLINE_CONTROL = (0x09, 0x0a, 0x0d)
# 监听线程连续异常多少次后交还控制台（避免一边刷屏一边空转）。
_MAX_READER_FAILURES = 5


def is_lower_hex_64(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    for char in value:
        if char not in _HEX:
            return False
    return True


def _nvs_read():
    """NVS 副本的读取（协议要求 2 的落点）。"""
    return nvs.read_blob(config.SERIAL_LINK_NVS_NAMESPACE, config.SERIAL_LINK_NVS_KEY)


def _nvs_write(key):
    """NVS 副本的写入；返回 None 表示成功，否则返回失败原因。"""
    return nvs.write_blob(config.SERIAL_LINK_NVS_NAMESPACE, config.SERIAL_LINK_NVS_KEY, key)


class KeyStore:
    """bootstrap_key 的落盘：文件（注册实际读取处）+ NVS（协议要求 2）。"""

    def __init__(self, path=None):
        self.path = path or config.BOOTSTRAP_KEY_FILE
        self.nvs_error = None

    def read(self):
        data = load_json(self.path, None)
        if isinstance(data, dict):
            key = data.get("bootstrap_key")
            if isinstance(key, str) and key:
                return key
        return _nvs_read()

    def write(self, key, hardware_id):
        # 保留文件里已有的其它字段（例如 bootstrap_id），只覆盖这两项；
        # 走 tmp + rename + sync 的原子替换，不留「文件不存在」的掉电窗口。
        data = load_json(self.path, None)
        payload = dict(data) if isinstance(data, dict) else {}
        payload["hardware_id"] = hardware_id
        payload["bootstrap_key"] = key
        save_json_atomic(self.path, payload)
        self.nvs_error = _nvs_write(key)
        if self.nvs_error and getattr(config, "SERIAL_LINK_REQUIRE_NVS", False):
            raise OSError("NVS 写入失败: {}".format(self.nvs_error))
        return key


class SerialLink:
    """协议状态机。读流/写流可注入，桌面单测不需要真串口。"""

    def __init__(self, hardware_id, store=None, stream=None, reader=None):
        self.hardware_id = hardware_id
        self.store = store if store is not None else KeyStore()
        self.stream_out = stream if stream is not None else sys.stdout
        self.stream_in = reader if reader is not None else sys.stdin
        self.buffer = bytearray()
        self.discarding = False
        self.released = False
        self.release_reason = ""
        self.threaded = False
        self.counters = {"lines": 0, "written": 0, "errors": 0}

    # ---------------- 协议 ----------------

    def handle(self, payload):
        if not isinstance(payload, dict):
            return self._fail("请求必须是 JSON 对象")
        command = payload.get("cmd")
        if command == IDENTIFY:
            return {"ok": True, "hardware_id": self.hardware_id,
                    "model": config.HARDWARE_MODEL, "firmware": config.FIRMWARE_VERSION}
        if command == WRITE_KEY:
            return self._write_key(payload.get("key"))
        if command == READ_KEY:
            return {"ok": True, "key": self.store.read()}
        return self._fail("未知 cmd: {}".format(command))

    def _write_key(self, key):
        if not is_lower_hex_64(key):
            # 原样传输、不做加工（要求 4），所以也不接受需要加工的输入：
            # 注册链路要求的就是 64 位小写 hex，这里放过等于把失败推到注册阶段。
            return self._fail("key 必须是 64 位小写 hex 字符串")
        try:
            self.store.write(key, self.hardware_id)
        except Exception as error:
            return self._fail("落盘失败: {}".format(error))
        # 落盘确认：现读一遍，与写入值逐字符比对后才回 ok（要求 2：写完才回）。
        if self.store.read() != key:
            return self._fail("读回校验失败，key 未被确认写入")
        self.counters["written"] += 1
        return {"ok": True}

    @staticmethod
    def _fail(message):
        return {"ok": False, "message": message}

    # ---------------- 字节流 ----------------

    def feed(self, data):
        """喂入原始字节，处理其中所有完整行；返回处理的行数。

        真机上 `sys.stdin.read(1)` 返回的是 **str**（不是 bytes）—— MicroPython 的
        `bytes(str)` 不接受没有 encoding 的参数，直接抛 TypeError 会把监听线程打死。
        桌面单测注入 bytes，照不出这个问题，所以两种都要接。
        """
        if self.released:
            return 0
        if isinstance(data, str):
            data = data.encode("utf-8")
        handled = 0
        for byte in bytes(data):
            if byte < 0x20 and byte not in _INLINE_CONTROL:
                self.release("收到裸控制字符 0x%02x" % byte)
                return handled
            if self.discarding:
                # 超长行的剩余字节必须丢到行尾为止，否则残余会粘到下一条命令上，
                # 把后面那条合法命令也解析坏（真机上是「一直不发换行」的垃圾流）。
                if byte in _LINE_END:
                    self.discarding = False
                continue
            if byte in _LINE_END:
                line = bytes(self.buffer)
                self.buffer = bytearray()
                if line.strip(b"\r"):
                    self._respond(line.strip(b"\r"))
                    handled += 1
                continue
            self.buffer.append(byte)
            if len(self.buffer) > config.SERIAL_LINK_MAX_LINE_BYTES:
                self.buffer = bytearray()
                self.discarding = True
                self._emit(self._fail("命令超过单行上限，已丢弃"))
        return handled

    def _respond(self, line):
        try:
            payload = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError):
            self.counters["errors"] += 1
            self._emit(self._fail("无法解析为 JSON"))
            return
        response = self.handle(payload)
        if not response.get("ok"):
            self.counters["errors"] += 1
        self.counters["lines"] += 1
        self._emit(response)

    def _emit(self, response):
        # 响应必须走真实 USB-CDC：日志已改道（见 ondeskbot/log.py），这里不会与日志打架。
        try:
            self.stream_out.write(json.dumps(response) + "\n")
            flush = getattr(self.stream_out, "flush", None)
            if flush:
                flush()
        except Exception as error:
            log("[串口协议] 响应写出失败: {}".format(error))

    def release(self, reason=""):
        if self.released:
            return
        self.released = True
        self.release_reason = reason
        self.discarding = False
        self.buffer = bytearray()
        log("[串口协议] 已交还控制台（{}）；本次上电不再监听，需断电重启才恢复".format(reason))

    # ---------------- 驱动 ----------------

    def _reader_loop(self):
        failures = 0
        while not self.released:
            try:
                chunk = self.stream_in.read(1)
                if not chunk:
                    sleep_ms(10)
                    continue
                self.feed(chunk)
                failures = 0
            except Exception as error:
                # 线程一死，串口协议就静默失联，而且**没有任何外部症状** ——
                # 之前就是这个：`bytes(str)` 抛 TypeError，线程默默没了，命令全超时。
                # 所以这里不但要兜住，还要在连续失败后明确留下痕迹。
                failures += 1
                log("[串口协议] 读写异常({}/{}): {}: {}".format(
                    failures, _MAX_READER_FAILURES, type(error).__name__, error))
                if failures >= _MAX_READER_FAILURES:
                    self.release("连续异常 {}".format(failures))
                    return
                sleep_ms(200)

    def start(self):
        """起监听线程；固件没编 _thread 时返回 False（由 service_tick() 兜底）。"""
        if not config.SERIAL_LINK_ENABLED:
            return False
        try:
            import _thread
        except ImportError:
            return False
        _thread.start_new_thread(self._reader_loop, ())
        self.threaded = True
        return True

    def service_tick(self):
        """主循环兜底：只有「没起线程、也没交还控制台」时才轮询。

        起了线程还在这里读，两边会同时抢同一个 stdin 把命令读花。
        """
        if self.threaded or self.released:
            return 0
        return self.poll()

    def poll(self, budget_ms=20):
        """无 _thread 时的兜底：主循环调用。门户等长阻塞期间会无响应。"""
        import select

        started = ticks_ms()
        handled = 0
        while not self.released and ticks_diff(ticks_ms(), started) < budget_ms:
            try:
                ready = select.select([self.stream_in], [], [], 0)
            except Exception:
                return handled
            if not ready or not ready[0]:
                return handled
            try:
                chunk = self.stream_in.read(1)
            except Exception:
                return handled
            if not chunk:
                return handled
            handled += self.feed(chunk)
        return handled


def install(hardware_id, store=None, stream=None, reader=None):
    """按配置装配并启动；返回 SerialLink 供 main.py 在 KeyboardInterrupt 时 release()。"""
    link = SerialLink(hardware_id, store=store, stream=stream, reader=reader)
    if not config.SERIAL_LINK_ENABLED:
        return link
    if not link.start():
        log("[串口协议] 无 _thread，改由主循环轮询（长阻塞期间会短暂无响应）")
    return link
