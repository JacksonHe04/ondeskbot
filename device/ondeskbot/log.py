"""运行日志改道：USB-CDC 只留给串口接入协议。

串口接入协议 v1 要求 1（「上电即监听，不抢发日志」）—— 该串口上出现任何非 JSON 行的
字节都会污染主机的行解析，所以固件自身的输出一个都不能落在 USB-CDC 上。

为什么不是「把 sys.stdout 换成黑洞」：MicroPython 的 print() 直接写 HAL stdout
（`mp_hal_stdout_tx_strn`），并不经过 sys.stdout，换 sys.stdout 拦不住它。因此只能把
调用点本身换掉 —— 全仓的 `print(` 一律改为本模块的 `log(`。

去向由 config 决定：
- `SERIAL_LINK_LOG_UART0 = True`：写 UART0（默认 GPIO43/44，与 Arduino 的 UART1 不冲突）；
- `False`：直接丢弃（现场没有接 UART0 时零开销）。

UART0 初始化失败（引脚被占/固件裁剪）不能让设备打挂，只降级为丢弃。
"""

import config

_uart = None
_uart_ready = False


def _target():
    global _uart, _uart_ready
    if _uart_ready:
        return _uart
    _uart_ready = True
    if not getattr(config, "SERIAL_LINK_LOG_UART0", False):
        return None
    try:
        import machine

        _uart = machine.UART(
            0,
            baudrate=config.SERIAL_LINK_LOG_BAUD,
            tx=config.SERIAL_LINK_LOG_TX_PIN,
            rx=config.SERIAL_LINK_LOG_RX_PIN,
        )
    except Exception:
        _uart = None
    return _uart


def write(text):
    """把一段文本写到日志去向；任何失败都静默（日志不能反过来把服务打挂）。"""
    uart = _target()
    if uart is None or not text:
        return
    try:
        uart.write(text)
    except Exception:
        pass


def log(*args, **kwargs):
    """print() 的替代品，签名子集：sep / end。

    刻意不实现 `file=`：日志只有一个去向，调用方不该再指定流。
    """
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    if not args:
        write(end)
        return
    write(sep.join([str(item) for item in args]) + end)


# 兼容按 `from .log import log` 之外再取一个别名阅读的写法（如 logger.log(...)）。
logger = log
