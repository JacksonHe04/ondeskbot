"""设备用 MQTT 3.1.1 最小子集：QoS0/1、显式 SUBACK、retain 元数据。

单线程、有界接收帧；不支持 QoS2/持久离线会话。上线前须真实固件/Broker 验收。
"""
import socket
import config
from ..compat import sleep_ms, sock_read, sock_write, ticks_diff, ticks_ms
from ..errors import DeviceError
from ..tls import wrap_socket


def u16(value):
    return bytes((value >> 8, value & 255))


def field(value):
    value = value.encode() if isinstance(value, str) else value
    return u16(len(value)) + value


class MqttClient:
    def __init__(self, host, port, identity, callback, ca_file=None):
        self.host, self.port, self.identity = host, port, identity
        self.callback, self.ca_file = callback, ca_file
        self.sock = None
        # 保留 TLS 握手前的原 TCP socket：部分固件的 SSLSocket 没有 settimeout，
        # 恢复读超时必须回到这一层设置。
        self.raw = None
        self.buffer = bytearray()
        self.packet_id = 0
        self.waiting = None
        self.answer = None
        self.last_send = ticks_ms()
        self.ping_at = None
        self.last_read_size = 0

    def _id(self):
        self.packet_id = self.packet_id % 65535 + 1
        return self.packet_id

    def _send(self, op, body=b""):
        length = len(body)
        prefix = bytearray([op])
        while True:
            digit = length % 128
            length //= 128
            prefix.append(digit | (128 if length else 0))
            if not length:
                break
        data = bytes(prefix) + body
        sent = 0
        start = ticks_ms()
        while sent < len(data):
            if ticks_diff(ticks_ms(), start) > config.MQTT_IO_TIMEOUT_S * 1000:
                raise OSError("MQTT write timeout")
            count = sock_write(self.sock, data[sent:])
            if not count:
                raise OSError("MQTT closed while writing")
            sent += count
        self.last_send = ticks_ms()

    def _exchange(self, op, body, expected, identifier=0):
        self.waiting, self.answer = (expected, identifier), None
        try:
            self._send(op, body)
            start = ticks_ms()
            while self.answer is None:
                self.check_msg()
                if ticks_diff(ticks_ms(), start) >= config.MQTT_IO_TIMEOUT_S * 1000:
                    raise OSError("MQTT ACK timeout")
                sleep_ms(5)
            return self.answer
        finally:
            self.waiting = None

    def connect(self, will_topic, will_message):
        raw = socket.socket()
        self.raw = raw
        self.sock = raw
        raw.settimeout(config.MQTT_HANDSHAKE_TIMEOUT_S)
        raw.connect(socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)[0][-1])
        self.sock = wrap_socket(raw, self.host, self.ca_file)
        # 握手完成后收紧到稳态 I/O 超时；check_msg() 每次读回后也会重新设这个值。
        if hasattr(self.sock, "settimeout"):
            self.sock.settimeout(config.MQTT_IO_TIMEOUT_S)
        # username/password + will retain/QoS1 + will flag + clean session.
        body = field(b"MQTT") + bytes((4, 0xee)) + u16(config.CLOUD_MQTT_KEEPALIVE_S)
        body += field(self.identity.device_id) + field(will_topic) + field(will_message)
        body += field(self.identity.device_id) + field(self.identity.device_secret)
        reply = self._exchange(0x10, body, 0x20)
        if len(reply) != 2 or reply[0] != 0:
            raise DeviceError("MQTT_PROTOCOL_ERROR", "CONNACK 格式/会话状态不正确")
        if reply[1]:
            raise DeviceError("MQTT_AUTH_REJECTED" if reply[1] in (4, 5) else "MQTT_CONNECT_REJECTED",
                              "Broker 拒绝连接", {"connack_code": reply[1]}, reply[1] == 3)

    def subscribe(self, topic, qos=1):
        identifier = self._id()
        answer = self._exchange(0x82, u16(identifier) + field(topic) + bytes((qos,)), 0x90, identifier)
        if len(answer) != 3 or answer[2] != qos:
            raise DeviceError("MQTT_SUBSCRIPTION_REJECTED", "SUBACK 未授予要求的 QoS1")
        return qos

    def publish(self, topic, message, qos=0, retain=False):
        identifier = self._id() if qos else 0
        body = field(topic) + (u16(identifier) if qos else b"")
        body += message.encode() if isinstance(message, str) else message
        if qos:
            self._exchange(0x30 | (qos << 1) | int(retain), body, 0x40, identifier)
        else:
            self._send(0x30 | int(retain), body)

    def _dispatch(self, op, body):
        kind = op & 0xf0
        if kind == 0x30:
            if len(body) < 2:
                raise OSError("MQTT truncated publish")
            length = body[0] * 256 + body[1]
            qos = (op >> 1) & 3
            end = 2 + length
            if qos > 1 or not length or len(body) < end + (2 if qos else 0):
                raise OSError("MQTT invalid publish")
            identifier = body[end:end+2] if qos else b""
            self.callback(body[2:end], body[end+(2 if qos else 0):], bool(op & 1), qos)
            if qos:
                self._send(0x40, identifier)
        elif op == 0xd0 and not body:
            self.ping_at = None
        elif self.waiting and op == self.waiting[0]:
            identifier = body[0] * 256 + body[1] if len(body) >= 2 and op != 0x20 else 0
            if identifier == self.waiting[1]:
                self.answer = body
        else:
            raise OSError("unexpected MQTT packet")

    def check_msg(self):
        # SSL 层可能已有解密数据；短非阻塞 read 避免仅 select 原 TCP 漏读。
        self.sock.setblocking(False)
        try:
            try:
                chunk = sock_read(self.sock, 2048)
            except OSError as error:
                if error.args and error.args[0] in (11, 35, 110, 2):
                    chunk = None
                else:
                    raise
        finally:
            # SSLSocket 是否支持 settimeout 因固件而异；不支持时回到原 TCP socket。
            target = self.raw if self.raw is not None else self.sock
            if hasattr(target, "settimeout"):
                target.setblocking(True)
                target.settimeout(config.MQTT_IO_TIMEOUT_S)
            else:
                self.sock.setblocking(True)
        if chunk == b"":
            raise OSError("MQTT connection closed")
        self.last_read_size = len(chunk) if chunk else 0
        if chunk:
            self.buffer.extend(chunk)
        if len(self.buffer) > config.MQTT_MAX_MESSAGE_BYTES + 2048:
            raise OSError("MQTT receive bound exceeded")
        while len(self.buffer) >= 2:
            remaining, multiplier, position = 0, 1, 1
            while True:
                if position >= len(self.buffer):
                    return
                byte = self.buffer[position]
                remaining += (byte & 127) * multiplier
                multiplier *= 128
                position += 1
                if position > 5 or remaining > config.MQTT_MAX_MESSAGE_BYTES:
                    raise OSError("MQTT frame too large")
                if not byte & 128:
                    break
            if len(self.buffer) < position + remaining:
                return
            op = self.buffer[0]
            body = bytes(self.buffer[position:position+remaining])
            # 部分固件不支持 bytearray 的切片删除，改用重新赋值。
            self.buffer = self.buffer[position + remaining:]
            self._dispatch(op, body)

    def keepalive(self):
        now = ticks_ms()
        if self.ping_at is not None and ticks_diff(now, self.ping_at) >= config.MQTT_IO_TIMEOUT_S * 1000:
            raise OSError("MQTT PINGRESP timeout")
        if self.ping_at is None and ticks_diff(now, self.last_send) >= config.CLOUD_MQTT_KEEPALIVE_S * 500:
            self._send(0xc0)
            self.ping_at = now

    def disconnect(self):
        try:
            if self.sock:
                self._send(0xe0)
        finally:
            if self.sock:
                self.sock.close()
            self.raw = None
