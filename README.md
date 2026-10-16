# OnDeskBot

与 Luke 协作的**硬件侧**仓库:桌面机器人的固件、设备协议与联调代码,并作为 48 小时 Hackathon 的初始 / 结束状态锚点。

## 定位

本仓库与 [FDE Anything](https://github.com/JacksonHe04/fde-anything) **相对独立**:

- **本仓库(硬件侧)**:设备固件、硬件协议、机器人本体相关代码——Luke 那边有自己的代码,设备寄到后在此联调
- **FDE Anything(平台侧)**:Agent as a Service 平台(Agent 装配、Channel、Open API)

两边各自演进,通过设备接入层对接。本仓库以 **git submodule** 形式挂在 FDE Anything 的 `ondeskbot/` 下,但保持独立的历史与版本节奏。

> **本仓库为 public 仓库**,任何提交都会公开——禁止提交敏感 / 私密信息(凭据、设备与网络密钥、个人信息等)。

## 背景

- Luke 已提供硬件侧原始代码,作为**参考与备份**放入 `_satellites/`(Git 忽略,不改动);我们在其基础上重新实现,而非直接整合。
- Luke 即将寄来实体设备。
- 本仓库同时是 **48h Hackathon**(Jackson × 嘉玮)的版本锚点:以起点 commit 记录比赛开始状态,结束时记录终点状态。

## 状态锚点

- 起点(比赛开始):tag `hackathon-start`
- 终点(比赛结束):待记录

## 目录

- `_satellites/`:Luke 提供的原始代码,只读参考、不入库
- 本仓库自身的实现:待补充
