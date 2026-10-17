"""`HttpClient(keep_alive=True)` 的四条契约。

为什么值得单独测：这条改的是"一张图一次完整 TLS 握手"这个**最大开销**，
但它同时引入一种新故障（复用了陈旧 socket）⇒ 必须钉住：
1. 复用——同一个 socket 连做两次请求（**不新建**）；
2. 陈旧连接——复用上出错时**丢弃并新建**，再试一次就成功（有界）；
3. 默认模式不变——仍然发 `Connection: close`、每次新建、用完就关（注册/其它路径不受影响）；
4. keep-alive + chunked——**靠终止块判完**，不能等 EOF（服务器不会关连接）。

测试用假 `usocket` 顶替真网络：代码里是 `import usocket as socket_module`，
所以往 `sys.modules` 里塞一个假的就够了（不碰真 socket、不需要网络）。

一个容易踩的细节：假连接是**按"每次 read 取下一个 chunk"**喂数据的，所以
"同一个 socket 服务两次请求"必须给**两个 chunk**（每个 chunk = 一条完整响应）。
"""
import sys
import types
import unittest

from ondeskbot.http_client import HttpClient

RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
CHUNKED = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"5\r\nhello\r\n0\r\n\r\n")


class FakeConnection:
    def __init__(self, chunks, idle_after_script=False):
        self.chunks = list(chunks)
        self.written = bytearray()
        self.closed = False
        self.fail_on_read = False
        self.idle_after_script = idle_after_script
        self.read_calls = 0

    def settimeout(self, value):
        pass

    def connect(self, address):
        pass

    def write(self, data):
        self.written.extend(bytes(data))
        return len(data)

    def read(self, size):
        self.read_calls += 1
        if self.fail_on_read:
            raise OSError(-104, "ECONNRESET")
        if self.chunks:
            return self.chunks.pop(0)
        if self.idle_after_script:
            raise AssertionError("脚本已喂完却还在读：keep-alive 下必须靠终止块判完")
        return b""

    def close(self):
        self.closed = True


class FakeSocketModule(types.ModuleType):
    SOCK_STREAM = 1

    def __init__(self, scripts):
        super().__init__("usocket")
        self.scripts = list(scripts)
        self.created = []

    def getaddrinfo(self, host, port, family=0, kind=0):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    def socket(self):
        chunks = self.scripts.pop(0) if self.scripts else []
        connection = FakeConnection(chunks)
        self.created.append(connection)
        return connection


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get("usocket")

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("usocket", None)
        else:
            sys.modules["usocket"] = self.saved

    def install(self, scripts):
        module = FakeSocketModule(scripts)
        sys.modules["usocket"] = module
        return module

    def test_keep_alive_reuses_the_same_socket(self):
        module = self.install([[RESPONSE, RESPONSE]])       # 一个 socket 服务两条响应
        client = HttpClient(keep_alive=True)
        first = client.request("GET", "http://api.example.test/readyz")
        second = client.request("GET", "http://api.example.test/readyz")
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(len(module.created), 1, "keep-alive 下不该新建第二个 socket")
        self.assertIn(b"Connection: keep-alive", bytes(module.created[0].written))
        self.assertFalse(module.created[0].closed, "池里的连接不该被关掉")

    def test_default_client_still_closes_each_time(self):
        module = self.install([[RESPONSE], [RESPONSE]])
        client = HttpClient()                                # 默认 keep_alive=False
        client.request("GET", "http://api.example.test/readyz")
        client.request("GET", "http://api.example.test/readyz")
        self.assertIsNone(client._pooled)
        self.assertEqual(len(module.created), 2, "默认模式每次新建（注册等路径行为不变）")
        for connection in module.created:
            self.assertIn(b"Connection: close", bytes(connection.written))
            self.assertTrue(connection.closed, "默认模式下用完必须关掉")

    def test_stale_pooled_socket_is_discarded_and_retried(self):
        module = self.install([[RESPONSE], [RESPONSE]])
        client = HttpClient(keep_alive=True)
        client.request("GET", "http://api.example.test/readyz")
        pooled = module.created[0]
        pooled.fail_on_read = True                           # 池里那条"变陈旧了"
        result = client.request("GET", "http://api.example.test/readyz")
        self.assertEqual(result.status, 200, "陈旧连接应被丢弃并用新连接重试一次")
        self.assertEqual(len(module.created), 2, "丢弃后应新建一个 socket")
        self.assertTrue(pooled.closed, "陈旧连接必须被关掉")

    def test_idle_pooled_socket_is_dropped(self):
        """闲太久就不再复用：与其必然先失败一次，不如直接新建（设备真正的复用只在连续动作之间）。"""
        from ondeskbot import http_client as module

        module.POOLED_IDLE_MAX_MS = 0        # 任何"闲置"都算过期
        try:
            fake = self.install([[RESPONSE], [RESPONSE]])
            client = HttpClient(keep_alive=True)
            client.request("GET", "http://api.example.test/readyz")
            pooled_connection = fake.created[0]
            client.request("GET", "http://api.example.test/readyz")
            self.assertEqual(len(fake.created), 2, "闲置的连接不该被复用（应当新建）")
            self.assertTrue(pooled_connection.closed, "被丢弃的闲置连接必须关掉")
        finally:
            module.POOLED_IDLE_MAX_MS = 45000

    def test_chunked_body_is_decoded_without_waiting_for_eof(self):
        module = self.install([[CHUNKED]])
        connection = None

        class IdleGuard(FakeSocketModule):
            def socket(self):
                nonlocal connection
                connection = super().socket()
                connection.idle_after_script = True
                return connection

        guard = IdleGuard([[CHUNKED]])
        sys.modules["usocket"] = guard
        client = HttpClient(keep_alive=True)
        result = client.request("GET", "http://api.example.test/readyz")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"hello")
        self.assertEqual(len(guard.created), 1)


if __name__ == "__main__":
    unittest.main()
