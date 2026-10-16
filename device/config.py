"""OnDeskBot 2.0 设备服务配置。

这里只保存非敏感默认值。device_secret、token 和 Wi-Fi 密码必须写入设备持久化
存储，禁止提交到源码仓库。
"""

# ===================== 项目与协议 =====================
PROJECT_NAME = "OnDeskBot-ESP32S3-DeviceService"
FIRMWARE_VERSION = "2.0.0-dev.5"
PROTOCOL_VERSION = "2.0-draft.4"
HARDWARE_MODEL = "OnDeskBot-ESP32S3-N16R8-OV5640"
COORDINATE_SYSTEM_VERSION = 6

# 云端协议未定，默认禁用外连 Transport，避免误连。
# 可选值: "disabled" | "memory" | "http_mock" | "mqtt"
TRANSPORT_KIND = "mqtt"
CLOUD_REGISTRATION_URL = "<CLOUD_BASE_URL>/v1/devices"
CLOUD_HTTP_BASE_URL = "<CLOUD_BASE_URL>"
CLOUD_MQTT_HOST = "<CLOUD_MQTT_HOST>"  # 云端 MQTT broker 地址（证书带 IP SAN）

CLOUD_MQTT_PORT = 8883
CLOUD_MQTT_TLS = True
CLOUD_CA_FILE = "/odb2/cloud-ca.pem"  # PEM；必须支持 SSLContext CERT_REQUIRED 和域名校验
MQTT_CONTRACT_CONFIRMED = True  # 双方冻结归档 ACK/连接代次契约并验收后启用
MQTT_MAX_MESSAGE_BYTES = 16384
MQTT_INGRESS_SIZE = 8  # 一条可执行，其余仅为已拒绝命令的回执入口
MQTT_IO_TIMEOUT_S = 5
# 连接/TLS 握手阶段的上限：真机上逐段握手偶发超过 5 秒，用 IO 超时去卡它会让
# "能连上的连接"被判失败（真机表现为要试 2~3 次才连上）。握手完成后仍回到 5 秒，
# 保证稳态下的读写不会被长时间阻塞。
MQTT_HANDSHAKE_TIMEOUT_S = 15
OUTBOX_SIZE = 32
COMMAND_JOURNAL_SIZE = 32
OUTBOX_RETRY_S = 5
MOCK_BOOTSTRAP_KEY_FILE = ""  # 仅开发注入的测试凭据文件路径
PRODUCTION_REGISTRATION_CONFIRMED = True  # 服务端确认本文档契约并完成验收后才开启
BOOTSTRAP_KEY_FILE = "/odb2/bootstrap.json"  # 单机 JSON: hardware_id + bootstrap_key；不得提交真实凭据
BOOTSTRAP_RECOVERY_CONFIRMED = False  # Studio 授权 + bootstrap HMAC 恢复契约冻结后开启
# ===================== 串口接入协议 v1 =====================
# 协议要求 1：USB-CDC 上电即静默、只回 JSON 行。固件自身的输出必须改道 —— 注意
# MicroPython 的 print() 直接写 HAL stdout（不经过 sys.stdout），所以只能改调用点，
# 全部走 ondeskbot/log.py 的 log()。
SERIAL_LINK_ENABLED = True  # 上电即在 USB-CDC 上监听 identify / write_key / read_key
SERIAL_LINK_LOG_UART0 = True  # 运行日志改道 UART0（GPIO43/44）；False = 直接丢弃
SERIAL_LINK_LOG_BAUD = 115200
SERIAL_LINK_LOG_TX_PIN = 43  # 与 Arduino 的 UART1（GPIO1/2）不冲突；不接也安全
SERIAL_LINK_LOG_RX_PIN = 44
# 单行上限：防「一直不发换行」的垃圾把内存撑爆；超限的行整行丢弃并回 ok:false。
SERIAL_LINK_MAX_LINE_BYTES = 4096
# 协议要求 2 写的是 NVS。本机固件的注册路径实际读 BOOTSTRAP_KEY_FILE，所以两处都写：
# NVS 是并列的durable 副本，文件是注册真正读的地方（详见 serial_link 顶部注释）。
SERIAL_LINK_NVS_NAMESPACE = "onboard"
SERIAL_LINK_NVS_KEY = "bootstrap_key"
SERIAL_LINK_REQUIRE_NVS = False  # True = NVS 写失败即拒绝；默认 False（文件成功即可用）
# 协议要求 3 是「幂等可重写」，所以**不做**「已绑定就拒绝 write_key」这类限制：
# 判据 4 的向导本来就要往一台已有凭据的设备上重写一把新的。
CLOUD_MQTT_KEEPALIVE_S = 60
CLOUD_MQTT_TOPIC_PREFIX = "ondeskbot/v2"
CLOUD_HEARTBEAT_INTERVAL_S = 60
# 以下两个阈值由 Server/Mock 按 last_seen 计算，设备只负责上报心跳。
CLOUD_SUSPECT_AFTER_S = 75
CLOUD_OFFLINE_AFTER_S = 90
CLOUD_RECONNECT_MIN_S = 2
# Broker 用旧 device_secret 拒绝时，允许自动重开激活事务的次数上限（「解除配对」用）。
CLOUD_REACTIVATION_MAX_ATTEMPTS = 3
# 只有连接**稳定存活**这么久才补充重激活配额。不能在 connect 成功那一刻就补：
# 否则 Broker 反复"接受后立刻踢掉"时，每一次成功连接都会重置配额，上限形同虚设，
# 设备会拿新 secret 无限重注册。60 秒 ≈ 一个心跳周期，足以区分"通了"和"抖一下"。
CLOUD_REACTIVATION_BUDGET_RESET_S = 60
# 被永久错误锁死（如缺少申请凭据）时，每隔这么久回头看一眼本地凭据有没有被
# 产线写进来；变了就解锁重试一次。避免"产线写好了、设备却等人工复位"。
BLOCKED_REARM_INTERVAL_S = 5
# 恢复出厂：擦除前等待"已执行"回执被归档的上限；超时就照擦，不能把设备永远吊住。
FACTORY_RESET_ACK_WAIT_S = 10
CLOUD_RECONNECT_MAX_S = 120
CLOUD_COMMAND_MAX_AGE_S = 30
ALLOW_INSECURE_DEV_TRANSPORT = False
# 单次 socket 操作（含 TLS 逐段握手）的上限。原先硬编码 5 秒，真机上跑整套服务时
# TLS 握手会超过它 → OSError -116 ETIMEDOUT，注册直接失败。放宽到 15 秒；整体请求
# 仍由各调用点的 timeout_s 兜住，不会无限等。
HTTP_SOCKET_TIMEOUT_S = 15
REQUIRE_TIME_SYNC_FOR_CLOUD = True
NTP_HOST = "pool.ntp.org"
TIME_VALID_AFTER_EPOCH_S = 1704067200  # 2024-01-01 UTC

# ===================== 持久化 =====================
DATA_DIR = "/odb2"
IDENTITY_FILE = DATA_DIR + "/identity.json"
WIFI_CRED_FILE = DATA_DIR + "/wifi.json"
CALIBRATION_FILE = DATA_DIR + "/calibration.json"
CAM_PROFILE_FILE = DATA_DIR + "/camera_profile.json"
MIGRATION_FILE = DATA_DIR + "/migration.json"

# 仅用于从 1.x 一次性导入。绝不迁移 LLM、Prompt 或 Agent 文件。
LEGACY_WIFI_CRED_FILE = "/wifi_cred.json"
LEGACY_SCREEN_ROI_FILE = "/screen_roi.json"
LEGACY_CAM_ARM_FILE = "/cam_arm_cal.json"
LEGACY_PHONE_POS_FILE = "/phone_pos.json"
LEGACY_CAM_PROFILE_FILE = "/camera_profile.json"

# ===================== 配网 =====================
WIFI_AP_SSID_PREFIX = "OnDeskBot-"
# ⚠️ 交付包里此口令已替换为占位符（口令不出我方本机）。
#    设备实际值请向设备侧获取，或在设备上读回。
WIFI_AP_PASSWORD = "<REPLACE_WITH_DEVICE_AP_PASSWORD>"  # 原型阶段统一密码；量产需升级为自动生成的一机一密
WIFI_AP_CHANNEL = 11
WIFI_AP_IP = "192.168.4.1"
WIFI_AP_SUBNET = "255.255.255.0"
WIFI_AP_WEB_PORT = 80
WIFI_AP_TIMEOUT_S = 600
WIFI_TX_POWER = 8.0
WIFI_STA_TIMEOUT_S = 30
WIFI_STA_MAX_RETRIES = 5
WIFI_RECONNECT_ATTEMPT_TIMEOUT_S = 15
WIFI_RECONNECT_MIN_S = 2
WIFI_RECONNECT_MAX_S = 120

# ===================== 摄像头 OV5640 =====================
CAM_JPEG_QUALITY = 10
CAM_FRAMESIZE = 10
CAM_TUNE_SAFE_JPEG_QUALITY = 20
CAM_TUNE_MAX_JPEG_QUALITY = 20
CAM_CAPTURE_MIN_INTERVAL_MS = 1500
CAM_RESTART_EVERY_N_CAPTURES = 80
# 拍照前把笔架（Z 向小车）收回原点。**为什么必须**：2026-10-02 真机 A/B 实证 ——
# 笔架停在工作位（x_steps=2200,y_steps=1440）时**挡死相机**，照片里只有笔架没有手机；
# 只把 X/Y 收回 (0,0)（几秒，不动 Z）视野立刻清空 ✓。而**确认照片走的是同一个 camera.capture**，
# 所以这个开关不只是在"帮模型看清"，它同时决定"人能不能在确认照片上看到手机" ——
# 关掉它，确认闸门就变成"看笔架点头"。
CAMERA_CAPTURE_PARK_FIRST = True  # False = 保持旧行为（拍照前不动机械臂）
CAMERA_CAPTURE_PARK_X_STEPS = 0   # 收回目标（绝对步数）；默认原点
CAMERA_CAPTURE_PARK_Y_STEPS = 0
# 抓**确认照片**（任务 pending、人还没点头）之前，先在屏幕上做一次上滑唤醒。
# 为什么只在 pending 期做：那时还没有任何任务状态可被破坏，一个上滑最多就是"唤醒/解锁"；
# 任务一旦 active，同样的手势会去划模型正在用的界面 ✗ —— 而且手机被频繁操作时本来也不会睡。
# 实测背景：手机静置会自动息屏（10-02 的确认照片里屏幕是暗的，人只能否决）。
CAMERA_CAPTURE_WAKE_WHEN_PENDING = True
CAMERA_CAPTURE_WAKE_SETTLE_MS = 1500  # 唤醒后等屏幕亮起/解锁动画走完再收回和拍照
CAM_RECOVER_AFTER_N_CAPTURE_FAILURES = 2
CAM_WARMUP_FRAMES = 3
CAM_UPGRADE_SETTLE_MS = 900
CAM_UPGRADE_VALIDATE_TRIES = 3
CAM_BASE64_CHUNK_SIZE = 3072
CAM_IMAGE_WIDTH = 1600
CAM_IMAGE_HEIGHT = 1200
CAM_VFLIP = False
CAM_HMIRROR = True
CAM_FORCE_JPEG = True

CAM_PIN_D0 = 21
CAM_PIN_D1 = 48
CAM_PIN_D2 = 8
CAM_PIN_D3 = 47
CAM_PIN_D4 = 14
CAM_PIN_D5 = 12
CAM_PIN_D6 = 11
CAM_PIN_D7 = 9
CAM_PIN_XCLK = 10
CAM_PIN_PCLK = 13
CAM_PIN_VSYNC = 6
CAM_PIN_HREF = 7
CAM_PIN_SIOD = 4
CAM_PIN_SIOC = 5
CAM_PIN_RESET = 15
CAM_PIN_PWDN = -1
CAM_XCLK_FREQ = 20000000

# 图像几何锁定。调参 API 不允许修改这些字段。
SCREEN_CROP_ROTATION = 0
SCREEN_CROP_HMIRROR = False

# ===================== UART / ArduinoDriverSCurveXY =====================
UART_ID = 1
UART_TX_PIN = 1
UART_RX_PIN = 2
UART_BAUD = 115200
UART_TIMEOUT_MS = 3000
Z_CLICK_HOLD_MS = 250
Z_DOUBLE_TAP_HOLD_MS = 60
LONG_PRESS_HOLD_MS = 3000
RAW_MOVE_MAX_X_STEPS = 3982
RAW_MOVE_MAX_Y_STEPS = 7080

# 「未归零 ⇒ 下一次动作前先归零」（B 设计）。
# 触发源（2026-10-03 实测四轮）：**横向拖动**（含合法的 system_gesture back/home ——
# 它们按平台约定必须贴边起手）后 Z 没回到限位 ⇒ `z_homed=0` ⇒ 后续需 Z 的动作全被联锁秒拒。
# ✅ **安全前提已确认（2026-10-04，读下位机源码 `ArduinoDriverSCurveXY.ino` `Controller_Home()`）**：
#    `Home` 的注释原文"**先抬 Z，确认笔尖离开屏幕后再移动 XY，避免 Home 过程划伤屏幕**" ⇒
#    自动归零不会带着压住的笔尖横扫屏幕 ⇒ 可以开。
MOTION_RECOVER_UNHOMED = True
# 任务级上限：一次 run 内最多自动归零几次（闸 3）。
# 2 → 5（2026-10-04，R26 实测）：48 步里触发 3 次（≈每 16 步 1 次）⇒ 上限 2 对长任务不够，
# 第 3 次触发就落回原生级联（R26 那唯一 1 次级联就是这么来的）。
# **为什么敢调大**：闸 3 只是"防失控循环"的**第二道保险**；真正断掉危险循环的是
# **闸 2（恢复用的 Home 自己失败 ⇒ 立即禁用恢复）**。单次代价实测 29–46 秒 ⇒ 上限 5 最坏多花 ~2.5–4 分钟，
# 换"整轮不报废"划算。**治本仍是降触发率**（下位机侧，见 二 的修法 A–E）。
# ⭐ 2026-10-05 改"形状"：**冷却式优先，计数只作安全天花板**（双方同意）。
# 理由：每次恢复本来就要 30–45 s ⇒ "两次恢复间隔 ≥ COOLDOWN" 几乎不额外限制，
# 却**随 run 变长自然放宽**；而真正防"恢复—再触发"死循环的是闸 2
# （恢复用的 Home 自己失败 ⇒ 立即禁用恢复）⇒ 计数上限只是第二道保险。
MOTION_RECOVERY_COOLDOWN_MS = 30000   # 两次自动归零之间至少间隔（毫秒）
MOTION_RECOVERY_LIMIT = 50            # 安全天花板（正常 run 远够不着；R27 是 6 次触发）

# ===================== 坐标与安全范围 =====================
COORD_INVERT_X = False
COORD_INVERT_Y = False
COORD_MARGIN = 0.02
CAM_ARM_X_MIN = 0.0
CAM_ARM_X_MAX = 1.0
CAM_ARM_Y_MIN = 0.0
CAM_ARM_Y_MAX = 1.0
PHONE_D_LEFT = 20.0
PHONE_D_RIGHT = 90.0
PHONE_D_TOP = 0.0
PHONE_D_BOTTOM = 144.0
ARM_TRAVEL_X = 99.55
ARM_TRAVEL_Y = 177.0
TOUCH_OFFSET_X_MM = 0.0
TOUCH_OFFSET_Y_MM = 0.0
CLICK_TARGET_OFFSET_X_MM = -1.0
CLICK_TARGET_OFFSET_Y_MM = 4.0
SWIPE_START_RATIO = 0.80
SWIPE_END_RATIO = 0.25

# ===================== 任务、安全会话与按键 =====================
TASK_CONFIRM_TIMEOUT_S = 120
MAINTENANCE_CONFIRM_TIMEOUT_S = 120
MAINTENANCE_SESSION_TTL_S = 300
CAMERA_TUNING_SESSION_TTL_S = 600
ROI_CALIBRATION_SESSION_TTL_S = 600
RESPONSE_CACHE_SIZE = 16

# BTN0/GPIO0 没有向用户开放，仅供研发烧录维护，不绑定运行时业务。
BUTTON_DEV_PIN = 0
BUTTON_REPROVISION_PIN = 39  # BTN1，长按重新配网
BUTTON_PAUSE_PIN = 40       # BTN2，安全暂停/继续
BUTTON_CONFIRM_PIN = 41     # BTN3，确认任务/维护会话/保存 Wi-Fi
BUTTON_ACTIVE_LEVEL = 0
BUTTON_DEBOUNCE_MS = 50
BUTTON_REPROVISION_HOLD_MS = 5000

LED_PIN = 38
