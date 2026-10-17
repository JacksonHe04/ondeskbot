"""Transport 与二进制制品上传接口。"""

from ..errors import DeviceError, TRANSPORT_NOT_CONFIGURED


class Transport:
    name = "base"

    def connect(self, identity):
        raise NotImplementedError

    def is_connected(self):
        return False

    def poll_command(self):
        return None

    def publish_response(self, response):
        raise NotImplementedError

    def publish_event(self, event):
        raise NotImplementedError

    def heartbeat(self, payload):
        raise NotImplementedError

    def upload(self, data, content_type, metadata, filename="artifact.bin"):
        raise NotImplementedError

    def close(self):
        pass


class NullTransport(Transport):
    name = "disabled"

    def connect(self, identity):
        return False

    def publish_response(self, response):
        raise DeviceError(TRANSPORT_NOT_CONFIGURED, "Transport 尚未配置")

    def publish_event(self, event):
        raise DeviceError(TRANSPORT_NOT_CONFIGURED, "Transport 尚未配置")

    def heartbeat(self, payload):
        return False

    def upload(self, data, content_type, metadata, filename="artifact.bin"):
        raise DeviceError(TRANSPORT_NOT_CONFIGURED, "照片上传 Transport 尚未配置")
