"""稳定的设备错误码。云端应按 code 判断，message 仅用于诊断。"""


class DeviceError(Exception):
    def __init__(self, code, message, details=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = bool(retryable)

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


INVALID_REQUEST = "INVALID_REQUEST"
UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
UNAUTHORIZED = "UNAUTHORIZED"
DEVICE_BUSY = "DEVICE_BUSY"
DEVICE_OFFLINE = "DEVICE_OFFLINE"
TASK_CONFIRMATION_REQUIRED = "TASK_CONFIRMATION_REQUIRED"
TASK_NOT_ACTIVE = "TASK_NOT_ACTIVE"
TASK_PAUSED = "TASK_PAUSED"
MAINTENANCE_CONFIRMATION_REQUIRED = "MAINTENANCE_CONFIRMATION_REQUIRED"
MAINTENANCE_SESSION_REQUIRED = "MAINTENANCE_SESSION_REQUIRED"
COMMAND_EXPIRED = "COMMAND_EXPIRED"
CAMERA_NOT_READY = "CAMERA_NOT_READY"
CAMERA_CAPTURE_FAILED = "CAMERA_CAPTURE_FAILED"
MOTION_FAILED = "MOTION_FAILED"
CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"
TRANSPORT_NOT_CONFIGURED = "TRANSPORT_NOT_CONFIGURED"

