"""严格 TLS：不支持 CA/主机名验证的固件拒绝联网，绝不降级。"""
import config
from .clock import clock_is_valid
from .errors import DeviceError


def wrap_socket(sock, hostname, ca_file=None):
    if not clock_is_valid():
        raise DeviceError("TIME_SYNC_REQUIRED", "TLS 需要可信设备时间")
    ca_file = ca_file or config.CLOUD_CA_FILE
    if not ca_file:
        raise DeviceError("TLS_CONFIGURATION_REQUIRED", "缺少可信 CA 文件")
    try:
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        if hasattr(context, "check_hostname"):
            context.check_hostname = True
        context.load_verify_locations(cafile=ca_file)
        return context.wrap_socket(sock, server_hostname=hostname)
    except (ImportError, AttributeError, TypeError):
        raise DeviceError("TLS_UNSUPPORTED", "当前固件不支持要求的 TLS 验证接口")
