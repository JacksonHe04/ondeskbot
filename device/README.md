# 设备端(OnDeskBot)

ESP32-S3 设备端的核心代码,负责在真实手机屏幕上完成物理交互。

## 能力

- **物理点击与滑动**:机械臂通过 Z 轴笔尖在手机屏幕上完成点击、双击、长按、滑动
- **屏幕观察**:OV5640 摄像头采集手机屏幕画面,供服务端理解当前界面
- **主控**:ESP32-S3(N16R8)运行 MicroPython,负责设备侧控制,以及与服务端的通信

## 组成

- `main.py` — MicroPython 入口,启动硬件能力服务
- `config.py` — 非敏感默认配置(引脚、超时、坐标范围等;凭据一律不进源码)
- `drivers/`
  - `arduino_serial.py` — 与下位机(Arduino + CNC Shield)的 UART 协议:`MoveClick` / `Drag` / `ZClick` / `Home` 等
  - `camera_manager.py` — OV5640 摄像头驱动
  - `wifi_manager.py` / `wifi_ap.py` / `wifi_status.py` — WiFi 连接与凭据存储、配网热点、状态码说明
- `ondeskbot/`
  - `motion.py` — 上层动作:点击 / 双击 / 长按 / 滑动,含未归零自动恢复
  - `coordinate_transform.py` — 屏幕比例坐标 ↔ 机械步数坐标
  - `capabilities.py` / `capability_contract.py` — 能力注册表与云端能力目录契约
  - `runtime_state.py` — 任务 / 暂停 / 取消 / 维护会话的状态机
  - `protocol.py` — 命令校验与响应封装
  - `security.py` / `clock.py` — 摘要与随机标识、可信设备时间
  - `transport/` — 可替换的云端通信接口与其内存实现
  - `storage.py` / `redact.py` — 原子 JSON 持久化、凭据字段屏蔽
  - `tls.py` / `diag.py` — 严格 TLS 包装、串口诊断行
  - 其余为动作层依赖的日志、错误与兼容模块

## 说明

本目录是设备服务的**核心模块**,用于说明设备侧能力;串口接入协议、摄像头调参、云端注册等实现未包含在此。
