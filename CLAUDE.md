# CLAUDE.md / AGENTS.md

> 本文件是 OnDeskBot 硬件侧仓库的协作与提交规则的唯一事实源;`AGENTS.md` 是指向本文件的软链接。
> **本仓库是 public 仓库**,下面的红线是硬约束。

## 这是什么

与 Luke 协作的**硬件侧**仓库:桌面机器人的固件、设备协议与联调代码。

- 与 [FDE Anything](https://github.com/JacksonHe04/fde-anything) 相对独立,以 git submodule 形式挂在它的 `ondeskbot/` 下
- Luke 的原始代码放在 `_satellites/`(Git 忽略),只读参考、不改动;我们在此之上重新实现
- 兼作 48h Hackathon 的初始 / 结束状态锚点

## 红线:禁止提交敏感 / 私密信息

**本仓库 public,任何一次提交都会立即公开,且难以彻底撤回。** 以下内容一律不得进入版本库:

- **凭据**:API Key、Token、密码、私钥、证书、JWT secret 及各类 secret
- **设备与网络**:WiFi SSID / 密码、MQTT 用户名 / 密码、设备序列号与唯一 ID、内网 IP、服务器地址与端口
- **个人信息**:手机号、邮箱、住址、身份证号,以及他人的姓名与联系方式
- **业务敏感**:未公开的客户 / 合作方信息、合同、报价
- **配置中的真实值**:`.env`、`*.local.*` 等一律由 `.gitignore` 兜住;需要样例时写 `.env.example` 占位

**提交前自查**:`git diff --cached` 逐行过一遍,拿不准的一律不提交。

## 提交规范

- 全英文 message,格式 `<type>(<module>): <short description>` + 要点
- 遵循 `git-commit` Skill

## 状态锚点

- `hackathon-start`:比赛起点
