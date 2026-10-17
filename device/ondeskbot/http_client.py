"""仅供注册、照片上传和开发 Mock 使用的轻量 HTTP 客户端。"""

import config

from .compat import json, sock_read, sock_write, ticks_diff, ticks_ms
from .cooperative import checkpoint

# 池里的连接"闲多久就别再用了"。为什么要这个：服务端（或其边缘）有自己的 idle 超时，
# 我们不知道它多长，而**复用一条对端已经关掉的连接**必然先失败一次（虽然有界重试兜住，
# 但白白多花一次 socket 超时）。设备真正的复用场景是**一串连续动作之间**（run 里
# 拍一张→点一下→再拍一张，间隔几秒到几十秒），所以只在"刚才用过"时才复用最划算。
POOLED_IDLE_MAX_MS = 45000


class HttpResult:
    def __init__(self, status, body=b"", headers=None):
        self.status = int(status)
        self.body = body or b""
        self.headers = headers or {}

    def json(self):
        raw = self.body.decode("utf-8") if isinstance(self.body, bytes) else self.body
        return json.loads(raw) if raw else {}


class HttpClient:
    """轻量 HTTP 客户端。`keep_alive=True` 时**复用连接**（默认关闭）。

    为什么要这个开关：真机上"每张照片一次失败"的失败点集中在**握手/边缘**，而不是
    数据本身 —— 证据是打服务端的健康检查端点（几 KB）也只有一半成功。
    而本客户端原来每次都发 `Connection: close` ⇒ **一张图 = 一次完整 TLS 握手**，
    在 ESP32 上这次握手的成本远高于传输本身。
    ⇒ 上传器改成复用连接（一次会话多次上传）；**注册与其他路径保持原状**（默认 False）。

    安全约束：
    - 复用连接上**任何异常都当作"陈旧 socket"**：丢弃、清池，再用新连接重试**一次**（有界）；
    - **keep-alive 模式下必须能自己判定响应结束** —— 服务器不会关连接，所以
      `Content-Length` 与 `chunked` 两种都要能算出"收完了"（chunked 由终止块判定）。
    """

    def __init__(self, keep_alive=False):
        self.keep_alive = bool(keep_alive)
        self._pooled = None          # (authority, connection, 放进池里的时刻)
        self._last_reused = False    # 上一跳是不是复用来的（决定出错后要不要丢弃重试）

    def close_pooled(self):
        """丢掉池里的连接（陈旧/出错/收尾时都走这里）。"""
        pooled = self._pooled
        self._pooled = None
        if pooled:
            try:
                pooled[1].close()
            except Exception:
                pass

    def _fresh_enough(self):
        """池里那条连接还够新吗（闲着超过 `POOLED_IDLE_MAX_MS` 就丢弃）。"""
        if not self._pooled:
            return False
        try:
            idle = ticks_diff(ticks_ms(), self._pooled[2])
        except Exception:
            return True
        if idle > POOLED_IDLE_MAX_MS:
            self.close_pooled()
            return False
        return True

    @staticmethod
    def _parse_headers(block):
        """把 HTTP 头块解析成 {小写名: 值}；没有冒号的行（如状态行）忽略。"""
        headers = {}
        for line in block.decode("utf-8", "ignore").split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.lower().strip()] = value.strip()
        return headers

    @staticmethod
    def _chunked_total_size(buffer, start):
        """chunked 响应"收完了"的总字节数（含终止块后的 CRLF）；还没收完返回 None。"""
        position = start
        while True:
            line_end = buffer.find(b"\r\n", position)
            if line_end < 0:
                return None
            size_text = bytes(buffer[position:line_end]).split(b";", 1)[0]
            try:
                size = int(size_text, 16)
            except ValueError:
                return None
            position = line_end + 2
            if size == 0:
                return position + 2 if len(buffer) >= position + 2 else None
            if len(buffer) < position + size + 2:
                return None
            position += size + 2

    @staticmethod
    def _send_all(connection, data):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            checkpoint()
            count = sock_write(connection, view[sent:sent+2048])
            if not count:
                raise OSError("HTTP socket send failed")
            sent += count

    @staticmethod
    def _decode_chunked(body):
        output = bytearray()
        position = 0
        while position < len(body):
            line_end = body.find(b"\r\n", position)
            if line_end < 0:
                raise ValueError("invalid chunked response")
            size_text = body[position:line_end].split(b";", 1)[0]
            size = int(size_text, 16)
            position = line_end + 2
            if size == 0:
                break
            output.extend(body[position:position + size])
            position += size + 2
        return bytes(output)

    def _socket_request(self, method, url, body, headers, timeout_s, allow_reuse=True):
        if "://" not in url:
            raise ValueError("invalid URL")
        scheme, remainder = url.split("://", 1)
        secure = scheme == "https"
        if scheme not in ("http", "https"):
            raise ValueError("unsupported URL scheme")
        slash = remainder.find("/")
        authority = remainder if slash < 0 else remainder[:slash]
        path = "/" if slash < 0 else remainder[slash:]
        if ":" in authority:
            host, port_text = authority.rsplit(":", 1)
            port = int(port_text)
        else:
            host, port = authority, (443 if secure else 80)

        try:
            import usocket as socket_module
        except ImportError:  # pragma: no cover - CPython normally uses urllib
            import socket as socket_module
        address = socket_module.getaddrinfo(host, port, 0, socket_module.SOCK_STREAM)[0][-1]
        connection = None
        if (self.keep_alive and allow_reuse and self._fresh_enough()
                and self._pooled[0] == authority):
            connection = self._pooled[1]
            self._pooled = None
        self._last_reused = connection is not None
        fresh = connection is None
        if fresh:
            connection = socket_module.socket()
        keep = False
        try:
            if fresh:
                if hasattr(connection, "settimeout"):
                    # 见 config.HTTP_SOCKET_TIMEOUT_S 的注释：5 秒会掐死真机上的 TLS 握手。
                    connection.settimeout(min(timeout_s, config.HTTP_SOCKET_TIMEOUT_S))
                connection.connect(address)
                if secure:
                    from .tls import wrap_socket
                    connection = wrap_socket(connection, host)

            merged = {
                "Host": host,
                "Connection": "keep-alive" if self.keep_alive else "close",
                "Content-Length": str(len(body)),
                "User-Agent": "OnDeskBot-MicroPython/2.0",
            }
            merged.update(headers)
            lines = ["{} {} HTTP/1.1".format(method, path)]
            lines.extend("{}: {}".format(name, value)
                         for name, value in merged.items())
            head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            self._send_all(connection, head)
            if body:
                self._send_all(connection, body)

            response = bytearray()
            expected_size = None
            chunked = False
            body_start = None
            while len(response) < 1024 * 1024:
                checkpoint()
                chunk = sock_read(connection, 2048)
                if not chunk:
                    break
                response.extend(chunk)
                marker = response.find(b"\r\n\r\n")
                if expected_size is None and marker >= 0:
                    body_start = marker + 4
                    head = self._parse_headers(bytes(response[:marker]))
                    if "content-length" in head:
                        expected_size = body_start + int(head["content-length"])
                        if expected_size > 1024 * 1024:
                            raise OSError("HTTP response too large")
                    if "chunked" in head.get("transfer-encoding", "").lower():
                        chunked = True
                if expected_size is not None and len(response) >= expected_size:
                    break
                if chunked and self.keep_alive and body_start is not None:
                    # keep-alive 下服务器不会关连接 ⇒ 只能靠终止块判定"收完了"
                    total = self._chunked_total_size(response, body_start)
                    if total is not None:
                        expected_size = total
                        break
            if expected_size is not None and len(response) < expected_size:
                raise OSError("incomplete HTTP response body")
            keep = self.keep_alive
        finally:
            if keep:
                self._pooled = (authority, connection, ticks_ms())
            else:
                try:
                    connection.close()
                except Exception:
                    pass

        marker = response.find(b"\r\n\r\n")
        if marker < 0:
            raise OSError("incomplete HTTP response")
        header_block = bytes(response[:marker])
        status_line = header_block.decode("utf-8", "ignore").split("\r\n", 1)[0]
        status = int(status_line.split(" ")[1])
        response_headers = self._parse_headers(header_block)
        response_body = bytes(response[marker + 4:])
        if response_headers.get("transfer-encoding", "").lower() == "chunked":
            response_body = self._decode_chunked(response_body)
        return HttpResult(status, response_body, response_headers)

    def request(self, method, url, body=None, headers=None, timeout_s=15):
        headers = dict(headers or {})
        body = body if body is not None else b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            return self._socket_request(method, url, body, headers, timeout_s)
        except OSError:
            # **复用的连接上出错** ⇒ 当作"陈旧 socket"：丢掉它、用**新连接**再试一次（有界）。
            # 注意判据是"上一跳是不是复用来的"，不是"池里还有没有连接" ——
            # `_socket_request` 一进来就把池里的连接取走了（取走后池是空的）。
            if not self.keep_alive or not self._last_reused:
                raise
            self._last_reused = False
            self.close_pooled()
            return self._socket_request(method, url, body, headers, timeout_s,
                                        allow_reuse=False)

    def json_request(self, method, url, payload=None, headers=None, timeout_s=15):
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        body = json.dumps(payload or {}).encode("utf-8")
        return self.request(method, url, body, merged, timeout_s)
