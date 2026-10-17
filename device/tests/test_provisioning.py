import json as _json
import socket as _socket
import threading
import time
import unittest

import config as _config

from ondeskbot.provisioning import (_html, _redirect, dns_reply,
                                    PORTAL_CLIENT_TIMEOUT_S, ProvisioningPortal)


def _query(name, qtype=1, qclass=1, qr=0, qdcount=1):
    """拼一个最小 DNS 查询报文。"""
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    flags = (0x8000 if qr else 0) | 0x0100
    header = b"\x12\x34" + flags.to_bytes(2, "big")
    header += qdcount.to_bytes(2, "big") + b"\x00\x00\x00\x00\x00\x00"
    return header + labels + qtype.to_bytes(2, "big") + qclass.to_bytes(2, "big")


class FakeClient:
    def __init__(self):
        self.data = b""

    def sendall(self, chunk):
        self.data += chunk


class ProvisioningPageTests(unittest.TestCase):
    def test_local_page_only_collects_wifi(self):
        page = _html()
        self.assertIn("Wi-Fi 配网", page)
        self.assertIn("Wi-Fi 密码", page)
        self.assertIn("联网并完成云端认证后标定", page)
        self.assertIn("不需要 BTN3", page)
        self.assertIn("重新配网不清除设备身份", page)
        self.assertIn("按设备上的 BTN3 按键确认", page)
        self.assertNotIn("/photo.jpg", page)
        self.assertNotIn("<canvas", page)
        self.assertNotIn("d_left", page)
        self.assertNotIn("d_right", page)


class CaptivePortalTests(unittest.TestCase):
    """手机系统的连通性探测要靠 302 + DNS 应答才会弹出配网页。"""

    def test_redirect_points_at_portal(self):
        client = FakeClient()
        _redirect(client, "http://192.168.4.1/")
        head = client.data.decode("utf-8")
        self.assertTrue(head.startswith("HTTP/1.1 302 Found\r\n"))
        self.assertIn("Location: http://192.168.4.1/\r\n", head)
        self.assertIn("Content-Length: 0\r\n", head)

    def test_dns_answer_resolves_every_name_to_hotspot(self):
        # 三家系统的探测域名都要能被解析到热点自身
        for name in ("connectivitycheck.gstatic.com",
                     "captive.apple.com",
                     "www.msftconnecttest.com"):
            reply = dns_reply(_query(name), "192.168.4.1")
            self.assertIsNotNone(reply, name)
            self.assertEqual(reply[0:2], b"\x12\x34")          # 事务号回显
            self.assertEqual(reply[2] & 0x80, 0x80)             # 是应答
            self.assertEqual(reply[3] & 0x0F, 0)                # RCODE = 0
            self.assertEqual(reply[6:8], b"\x00\x01")           # ANCOUNT = 1
            self.assertTrue(reply.endswith(b"\x00\x04\xc0\xa8\x04\x01"))  # 192.168.4.1

    def test_dns_question_is_echoed_so_client_matches_it(self):
        query = _query("captive.apple.com")
        reply = dns_reply(query, "192.168.4.1")
        self.assertIn(query[12:], reply)                        # 原问题段保留

    def test_dns_ignores_non_a_and_non_query(self):
        self.assertIsNone(dns_reply(_query("a.com", qtype=28), "192.168.4.1"))   # AAAA
        self.assertIsNone(dns_reply(_query("a.com", qclass=3), "192.168.4.1"))   # CH
        self.assertIsNone(dns_reply(_query("a.com", qr=1), "192.168.4.1"))       # 应答报文
        self.assertIsNone(dns_reply(_query("a.com", qdcount=0), "192.168.4.1"))
        self.assertIsNone(dns_reply(b"\x00\x01", "192.168.4.1"))                 # 截断报文
        self.assertIsNone(dns_reply(_query("a.com"), "not.an.ip"))               # 坏 IP

    def test_dns_handles_truncated_question(self):
        query = _query("verylongname.example.com")[:-6]
        self.assertIsNone(dns_reply(query, "192.168.4.1"))


# ---------- 配网流程端到端 ----------

class _FakeWifi:
    """STA 侧：扫描与凭据落盘。"""

    def __init__(self):
        self.saved = None

    def scan_networks(self):
        return [{"ssid": "AP1", "rssi": -40}]

    def save_credentials(self, ssid, password):
        self.saved = (ssid, password)


class _FakeAp:
    """AP 侧：热点生命周期。"""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start_ap(self):
        self.started += 1
        return True

    def stop_ap(self):
        self.stopped += 1

    def get_ap_ip(self):
        return "192.168.4.1"


class _FakeButtons:
    def __init__(self):
        self.events = []

    def poll(self):
        pass

    def consume(self):
        out, self.events = self.events, []
        return out


def _free_port():
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _request(port, method, path, body=None, timeout=3):
    c = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        payload = b"" if body is None else _json.dumps(body).encode()
        head = "{} {} HTTP/1.1\r\nHost: 192.168.4.1\r\nContent-Length: {}\r\n" \
               "Connection: close\r\n\r\n".format(method, path, len(payload))
        c.sendall(head.encode() + payload)
        chunks = b""
        while True:
            part = c.recv(4096)
            if not part:
                break
            chunks += part
        return chunks
    finally:
        c.close()


def _start_portal(portal, timeout_s=2):
    t = threading.Thread(target=portal.run, kwargs={"timeout_s": timeout_s},
                         daemon=True)
    t.start()
    time.sleep(0.4)          # 等服务器起来
    return t


class ProvisioningFlowTests(unittest.TestCase):
    def setUp(self):
        self._orig_port = _config.WIFI_AP_WEB_PORT
        self.port = _free_port()
        _config.WIFI_AP_WEB_PORT = self.port
        self.wifi = _FakeWifi()
        self.ap = _FakeAp()
        self.buttons = _FakeButtons()
        self.portal = ProvisioningPortal(self.wifi, self.ap, self.buttons)

    def tearDown(self):
        _config.WIFI_AP_WEB_PORT = self._orig_port

    def test_probe_paths_are_redirected_to_portal(self):
        """各家系统的探测路径都必须被 302 引导到配网页，否则手机不会弹页。"""
        _start_portal(self.portal)
        for path in ("/generate_204", "/gen_204", "/hotspot-detect.html",
                     "/library/test/success.html", "/ncsi.txt",
                     "/connecttest.txt", "/anything-else"):
            raw = _request(self.port, "GET", path).decode("utf-8", "ignore")
            self.assertTrue(raw.startswith("HTTP/1.1 302 Found"), path)
            self.assertIn("Location: http://192.168.4.1/", raw)

    def test_real_routes_still_work(self):
        _start_portal(self.portal)
        self.assertIn("Wi-Fi 配网",
                      _request(self.port, "GET", "/").decode("utf-8", "ignore"))
        self.assertIn("AP1",
                      _request(self.port, "GET", "/api/networks").decode("utf-8", "ignore"))
        self.assertIn("pending",
                      _request(self.port, "GET", "/api/status").decode("utf-8", "ignore"))

    def test_submit_only_pends_until_btn3(self):
        """提交后不得直接落盘：必须等 BTN3 物理确认。"""
        _start_portal(self.portal)
        raw = _request(self.port, "POST", "/api/config",
                       {"ssid": "AP1", "password": "pw"}).decode()
        self.assertIn("202", raw.split("\r\n")[0])
        self.assertIsNone(self.wifi.saved)              # 未按 BTN3 ⇒ 不写凭据
        self.assertEqual(self.portal.pending["ssid"], "AP1")

    def test_btn3_confirm_saves_and_exits(self):
        _start_portal(self.portal, timeout_s=5)
        _request(self.port, "POST", "/api/config", {"ssid": "AP1", "password": "pw"})
        self.buttons.events.append("confirm")
        for _ in range(40):                              # 等主循环取到按键事件
            if self.wifi.saved:
                break
            time.sleep(0.1)
        self.assertEqual(self.wifi.saved, ("AP1", "pw"))
        self.assertTrue(self.portal.confirmed)

    def test_hung_client_does_not_freeze_portal(self):
        """连上却不发数据的连接不得把门户卡死 —— 手机上的其它 App 会自动来连。"""
        _start_portal(self.portal, timeout_s=10)
        hung = _socket.create_connection(("127.0.0.1", self.port), timeout=3)
        time.sleep(0.3)                      # 让它先被 accept 掉
        start = time.time()
        raw = _request(self.port, "GET", "/api/networks",
                       timeout=PORTAL_CLIENT_TIMEOUT_S + 4).decode()
        elapsed = time.time() - start
        hung.close()
        self.assertIn("AP1", raw)
        self.assertLess(elapsed, PORTAL_CLIENT_TIMEOUT_S + 4,
                        "门户被僵死连接卡住了 %.1fs" % elapsed)

    def test_empty_ssid_rejected(self):
        _start_portal(self.portal)
        raw = _request(self.port, "POST", "/api/config",
                       {"ssid": "   ", "password": "pw"}).decode()
        self.assertIn("400", raw.split("\r\n")[0])
        self.assertIsNone(self.portal.pending)


if __name__ == "__main__":
    unittest.main()
