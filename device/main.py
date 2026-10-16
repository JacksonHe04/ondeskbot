"""MicroPython 主入口：只运行硬件能力服务，不运行 Agent 或模型。"""

from ondeskbot import serial_link
from ondeskbot.service import create_default_service


service = create_default_service()
# 串口接入协议 v1：上电即在 USB-CDC 上监听。运行日志已由 ondeskbot/log.py 改道到
# UART0/丢弃，不会污染主机对 JSON 行的解析。hardware_id 取同一个 DeviceIdentity，
# 保证 identify 回的就是将来注册上报的那一个（判据 2）。
service.serial_link = serial_link.install(service.identity.hardware_id)
try:
    service.run_forever()
except KeyboardInterrupt:
    # 进 REPL 之前先把串口让出去：监听线程会抢 sys.stdin，不让出的话
    # mpremote 既打断不了 main.py 也进不了 raw REPL。
    service.serial_link.release("KeyboardInterrupt")
    raise
