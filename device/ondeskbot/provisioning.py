"""仅在首次配网或 BTN1 长按后开放的本地 WPA2 配网页面。

依赖按职责拆成两个：`wifi` 负责 STA 扫描与凭据落盘，`ap` 负责热点生命周期
（起停与真实 IP）。两者都从外部注入，桌面测试不需要真无线网卡。
"""

import socket

import config

from .compat import json, ticks_add, ticks_diff, ticks_ms
from .log import log

# 已接受的客户端连接必须带超时。否则一个「连上却不发完整请求」的连接会让
# _read_request 永远阻塞，整个配网门户随之冻结 —— 表现为热点还在、页面打不开、
# 按键也完全没反应。手机上的其它 App 会自动来连，所以这在实际使用中很容易触发。
PORTAL_CLIENT_TIMEOUT_S = 5

# 最小 DNS 应答的固定字段。
_DNS_REPLY_FLAGS = b"\x81\x80"                      # 标准应答，RD/RA 置位
_DNS_COUNTS = b"\x00\x01\x00\x01\x00\x00\x00\x00"   # 1 问题 / 1 答案 / 0 权威 / 0 附加
_DNS_ANSWER_RR = (b"\xc0\x0c"                       # 名字指针 → 问题里的 QNAME
                  b"\x00\x01\x00\x01"               # A / IN
                  b"\x00\x00\x00\x3c"               # TTL 60s
                  b"\x00\x04")                      # 4 字节地址


def _html():
    return r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OnDeskBot 配网</title>
<style>body{font-family:system-ui;margin:0;background:#09111f;color:#e6eefb}main{max-width:640px;margin:auto;padding:18px}.card{background:#111d30;border:1px solid #2d405f;border-radius:12px;padding:16px;margin:12px 0}input,select,button{font-size:16px;padding:10px;border-radius:7px;border:1px solid #456;background:#0b1627;color:#fff;width:100%;margin:6px 0;box-sizing:border-box}button{background:#1769aa}.hint{color:#9db0cd;font-size:13px}.warn{color:#ffc66d}</style></head>
<body><main><h2>OnDeskBot 2.0 Wi-Fi 配网</h2><div class="card"><label>2.4GHz Wi-Fi</label><select id="ssid"></select><input id="manual" placeholder="列表中没有时手工输入 SSID"><input id="pw" type="password" placeholder="Wi-Fi 密码"><p class="hint">屏幕 ROI 将在设备联网并完成云端认证后标定，本页面不填写机械参数。普通 ROI 框选在云端确认，不需要 BTN3；调参和机械维护需要 BTN3 确认。重新配网不清除设备身份；若提示认证未配置，请联系研发完成配置，不必反复配网。</p></div>
<div class="card"><button id="submit" onclick="submitConfig()">提交 Wi-Fi</button><p id="msg" class="warn">提交后必须按设备上的 BTN3 按键确认，才会写入 Wi-Fi 配置。</p></div></main>
<script>
function say(t){document.getElementById('msg').textContent=t}
function req(method,url,body,cb){
  var x=new XMLHttpRequest();
  x.open(method,url,true);
  if(body)x.setRequestHeader('Content-Type','application/json');
  x.onreadystatechange=function(){
    if(x.readyState!==4)return;
    var d={};
    try{d=JSON.parse(x.responseText)||{}}catch(e){d={error:'响应无法解析（HTTP '+x.status+'）'}}
    cb(x.status,d);
  };
  x.onerror=function(){cb(0,{error:'网络请求失败，请确认仍连着配网热点'})};
  x.ontimeout=function(){cb(0,{error:'请求超时'})};
  x.send(body?JSON.stringify(body):null);
}
function init(){
  var s=document.getElementById('ssid');
  req('GET','/api/networks',null,function(st,d){
    (d.networks||[]).forEach(function(n){
      var o=document.createElement('option');o.value=n.ssid;
      o.textContent=n.ssid+' ('+n.rssi+'dBm)';s.appendChild(o);
    });
  });
  setInterval(status,1500);
}
function submitConfig(){
  var manual=document.getElementById('manual').value.trim();
  var ssid=manual||document.getElementById('ssid').value;
  if(!ssid){say('请先选择列表中的 Wi-Fi，或在下方手工填写名称');return}
  say('正在提交…');
  req('POST','/api/config',{ssid:ssid,password:document.getElementById('pw').value},
    function(st,d){say(d.message||d.error||('提交失败 (HTTP '+st+')'))});
}
function status(){
  req('GET','/api/status',null,function(st,d){
    if(d.confirmed)say('设备已确认，正在连接 Wi-Fi…');
    else if(d.pending)say('设置待确认：请按设备上的 BTN3 按键');
  });
}
init();</script></body></html>'''


def _response(client, status, content_type, body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    reason = {200: "OK", 202: "Accepted", 400: "Bad Request",
              404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    header = ("HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n"
              "Connection: close\r\nCache-Control: no-store\r\n\r\n").format(
                  status, reason, content_type, len(body))
    client.sendall(header.encode("utf-8"))
    client.sendall(body)


def _json_response(client, status, payload):
    _response(client, status, "application/json; charset=utf-8", json.dumps(payload))


def _redirect(client, location):
    """302 到配网页。各家手机系统正是靠「探测请求被重定向」判定网络需要登录，
    进而自动弹出配网页面；返回 404 只会让系统认定网络异常、什么都不弹。"""
    header = ("HTTP/1.1 302 Found\r\nLocation: {}\r\nContent-Length: 0\r\n"
              "Connection: close\r\nCache-Control: no-store\r\n\r\n").format(location)
    client.sendall(header.encode("utf-8"))


def dns_reply(query, ip):
    """最小 DNS 应答：把任意 A 查询都解析为配网热点自身的 IP。

    手机连上热点后会立刻用固定域名做连通性探测（Android 用
    connectivitycheck.gstatic.com、iOS 用 captive.apple.com、Windows 用
    www.msftconnecttest.com）。纯 AP 模式没有上游 DNS，ESP32 自身也不提供解析，
    这些探测请求根本到不了我们的 HTTP 端口，也就无法被重定向。把查询统一应答为
    热点 IP 之后，探测会打到配网页面上。
    返回要发送的应答字节，或 None 表示不处理该报文。
    """
    if len(query) < 12 or query[2] & 0x80:      # 太短，或本身是应答报文
        return None
    if query[4] != 0 or query[5] != 1:          # 只处理单问题查询
        return None
    pos = 12
    while pos < len(query) and query[pos]:      # 跳过 QNAME
        pos += query[pos] + 1
    pos += 1
    if pos + 4 > len(query):
        return None
    qtype = query[pos] * 256 + query[pos + 1]
    qclass = query[pos + 2] * 256 + query[pos + 3]
    if qtype != 1 or qclass != 1:               # 只答 A 记录 / IN 类
        return None
    try:
        packed = bytes(int(part) for part in ip.split("."))
    except ValueError:
        return None
    question = query[12:pos + 4]
    return (query[0:2] + _DNS_REPLY_FLAGS + _DNS_COUNTS +
            question + _DNS_ANSWER_RR + packed)


def _read_request(client):
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 16384:
        chunk = client.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        raise ValueError("HTTP 请求头不完整")
    header_text = bytes(data[:marker]).decode("utf-8", "ignore")
    lines = header_text.split("\r\n")
    first = lines[0].split(" ")
    method, path = first[0], first[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.lower().strip()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    body = bytearray(data[marker + 4:])
    while len(body) < length:
        chunk = client.recv(min(1024, length - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    return method, path, bytes(body[:length])


class ProvisioningPortal:
    def __init__(self, wifi, ap, buttons, now_provider=None):
        self.wifi = wifi
        self.ap = ap
        self.buttons = buttons
        self._now = now_provider or ticks_ms
        self.pending = None
        self.confirmed = False
        self.networks = []

    def _serve(self, client, address, portal_ip, deadline):
        """处理一个已接受的 HTTP 连接；任何异常都回 400，绝不把门户带崩。"""
        try:
            client.settimeout(PORTAL_CLIENT_TIMEOUT_S)
            method, path, body = _read_request(client)
            if path != "/api/status":        # 状态轮询每秒一次，不记以免刷屏
                log("[配网] {} {} 来自 {}".format(
                    method, path, address[0] if address else "?"))
            if method == "GET" and path == "/":
                _response(client, 200, "text/html; charset=utf-8", _html())
            elif method == "GET" and path == "/api/networks":
                _json_response(client, 200, {"networks": self.networks})
            elif method == "GET" and path == "/api/status":
                _json_response(client, 200, {
                    "pending": bool(self.pending), "confirmed": self.confirmed,
                    "timeout_s": max(0, ticks_diff(deadline, self._now()) // 1000),
                })
            elif method == "POST" and path == "/api/config":
                payload = json.loads(body.decode("utf-8"))
                ssid = str(payload.get("ssid", "")).strip()
                if not ssid:
                    raise ValueError("SSID 不能为空")
                self.pending = {
                    "ssid": ssid,
                    "password": str(payload.get("password", "")),
                }
                _json_response(client, 202, {
                    "pending": True, "message": "请按设备 BTN3 确认保存",
                })
            else:
                # 未定义的路径一律 302 到配网页。手机系统对连通性探测
                # 请求（Android 的 /generate_204、iOS 的 /hotspot-detect.html、
                # Windows 的 /ncsi.txt 等）看到重定向，就会自动弹出配网页。
                _redirect(client, "http://" + portal_ip + "/")
        except Exception as error:
            try:
                _json_response(client, 400, {"error": str(error)})
            except Exception:
                pass
        finally:
            client.close()

    def run(self, timeout_s=None):
        timeout_s = int(timeout_s or config.WIFI_AP_TIMEOUT_S)
        # 先扫描再进入纯 AP，避免 STA 扫描改变配网热点信道。
        self.networks = self.wifi.scan_networks()
        self.ap.start_ap()
        server = socket.socket()
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        server.bind(("0.0.0.0", config.WIFI_AP_WEB_PORT))
        server.listen(2)
        server.settimeout(0.25)
        # 取热点实际生效的 IP：start_ap() 里自定义 IP 失败时会回退到默认值。
        portal_ip = config.WIFI_AP_IP
        try:
            actual_ip = getattr(self.ap, "get_ap_ip", None)
            if actual_ip:
                portal_ip = actual_ip() or portal_ip
        except Exception:
            pass
        # 最小 DNS 应答：让手机的连通性探测能解析到热点自身，否则探测请求
        # 到不了本机 HTTP 端口，自动弹页也就无从触发。
        dns = None
        try:
            dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            dns.bind(("0.0.0.0", 53))
            dns.settimeout(0.05)
        except Exception as error:
            log("[配网] DNS 应答未启用: {}".format(error))
            if dns is not None:
                try:
                    dns.close()
                except Exception:
                    pass
                dns = None
        # 按键靠定时器中断锁存；只靠主循环轮询的话，一旦某个客户端把循环拖慢，
        # 按键就会被漏掉。门户若发现定时器没跑就自己启一个，退出时再还回去。
        started_timer = False
        try:
            if hasattr(self.buttons, "is_running") and not self.buttons.is_running():
                started_timer = bool(self.buttons.start())
        except Exception:
            started_timer = False
        deadline = ticks_add(self._now(), timeout_s * 1000)
        result = None
        try:
            while ticks_diff(deadline, self._now()) > 0:
                if dns is not None:
                    try:
                        query, address = dns.recvfrom(256)
                        reply = dns_reply(query, portal_ip)
                        if reply:
                            dns.sendto(reply, address)
                    except OSError:
                        pass
                    except Exception:
                        pass
                self.buttons.poll()
                for event in self.buttons.consume():
                    if event == "confirm" and self.pending:
                        self.wifi.save_credentials(
                            self.pending["ssid"], self.pending["password"])
                        self.confirmed = True
                        result = dict(self.pending)
                        break
                if result:
                    break
                try:
                    client, address = server.accept()
                except OSError:
                    continue
                self._serve(client, address, portal_ip, deadline)
        finally:
            server.close()
            if started_timer:
                try:
                    self.buttons.stop()
                except Exception:
                    pass
            if dns is not None:
                try:
                    dns.close()
                except Exception:
                    pass
            self.ap.stop_ap()
        return result
