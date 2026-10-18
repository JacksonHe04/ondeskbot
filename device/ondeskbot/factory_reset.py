"""恢复出厂：按白名单擦除设备内数据，回到"未绑定"状态。

语义（与设备侧冻结的白名单一致）：

- **擦数据**：申请凭据、双槽身份记录、发件箱、标定、相机配置、Wi-Fi 凭据、1.x 迁移标记。
- **绝不动**：四张 CA/根证书（`cloud-ca.pem` / `device-ca.pem` / `gts-root-r4.pem` /
  `isrg-root-x1.pem`）。擦掉它们设备就连不上云端，也就永远无法在线上报
  "我已恢复出厂"，验收会退化成纯现场。
- **未在白名单也未在保护名单里的文件一律不擦**，只记进报告的 `unclassified`：
  宁可让人看见"多了个文件"，也不要在现场把设备擦成砖。

掉电安全：先原子写 `factory_reset.pending` 标记，再擦；重启后若标记还在就把擦除
幂等重跑一遍再清标记。这样"擦了一半掉电"和"刚记下意图就掉电"两种情况都会收敛到
同一个终态。
"""

import os

import config
from .compat import json
from . import nvs
from .storage import ensure_dir, remove_if_exists

MARKER_FILE = config.DATA_DIR + "/factory_reset.pending"


def _marker_basename():
    """按调用时刻取，别在 import 时定死——测试会把 MARKER_FILE 指到临时目录。"""
    return MARKER_FILE.rsplit("/", 1)[-1]

# 出厂要擦掉的数据文件（含原子写入可能留下的 .tmp 残片）。
DATA_FILES = (
    "bootstrap.json", "bootstrap.json.tmp",
    "identity.json", "identity.json.tmp",
    "identity.json.credentials.0", "identity.json.credentials.1",
    "identity.json.outbox.0", "identity.json.outbox.1",
    # 命令日志（DurableRecord，identity.path + ".commands"）：留着会把上一代的
    # request_id/结果带进新绑定，且 uncertain 状态会永久挡住副作用命令。
    "identity.json.commands.0", "identity.json.commands.1",
    "calibration.json", "calibration.json.tmp",
    "camera_profile.json", "camera_profile.json.tmp",
    "wifi.json", "wifi.json.tmp",
    "migration.json",
)

# 排障期留下的 CA 探针文件；顺手清掉。
PROBE_FILES = ("_ca_0.pem", "_ca_1.pem", "_ca_2.pem", "_only_gts.pem")

# 1.x 遗留在**文件系统根**的数据/配置。必须擦：不擦的话恢复出厂后第一次开机，
# `migrate_legacy_files()` 会把它们原样导回 /odb2（真机实测踩到过：擦完重启又从
# /wifi_cred.json 导回了上一代的 Wi-Fi，设备先去连一个不存在的热点，5 次失败才
# 退回配网热点）。`llm_config.json` 可能含上一代的模型密钥，同样不能留。
# 只擦数据/配置，**不动 1.x 的程序文件**（agent.py / prompts.py / llm_client.py…）。
LEGACY_ROOT_FILES = (
    config.LEGACY_WIFI_CRED_FILE,       # /wifi_cred.json
    config.LEGACY_SCREEN_ROI_FILE,      # /screen_roi.json
    config.LEGACY_CAM_ARM_FILE,         # /cam_arm_cal.json
    config.LEGACY_PHONE_POS_FILE,       # /phone_pos.json
    config.LEGACY_CAM_PROFILE_FILE,     # /camera_profile.json
    "/llm_config.json",
)

# 基础设施：任何情况下都不擦。
PROTECTED_FILES = ("cloud-ca.pem", "device-ca.pem", "gts-root-r4.pem", "isrg-root-x1.pem")


def _erase_list():
    overlap = set(PROTECTED_FILES) & (set(DATA_FILES) | set(PROBE_FILES))
    if overlap:
        raise ValueError("擦除清单与保护清单重叠: {}".format(sorted(overlap)))
    return tuple(DATA_FILES) + tuple(PROBE_FILES)


def planned_clears():
    """将要擦除的清单，用于命令回执。

    **回执发在擦除之前**（这是有意的：擦掉发件箱会连这条回执一起丢），所以回执里
    只能是"计划擦哪些"，不可能是"实际擦掉了哪些"。实际结果打在串口
    `[云] factory_reset_erased ...` 那一行。
    """
    return sorted(list(_erase_list()) + list(LEGACY_ROOT_FILES))


def pending():
    return _exists(MARKER_FILE)


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError as error:
        return bool(error.args and error.args[0] != 2)


def mark_pending():
    """原子记下"要恢复出厂"的意图；重跑是幂等的。"""
    ensure_dir(config.DATA_DIR)
    temporary = MARKER_FILE + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(json.dumps({"requested_at_ms": _now_ms()}))
        handle.flush()
        if hasattr(os, "fsync"):
            os.fsync(handle.fileno())
    if hasattr(os, "sync"):
        os.sync()
    os.rename(temporary, MARKER_FILE)
    if hasattr(os, "sync"):
        os.sync()


def _now_ms():
    try:
        from .compat import epoch_ms
        return epoch_ms()
    except Exception:
        return 0


def erase_all():
    """幂等擦除；返回报告。

    关键约束：**删不掉的文件绝不能被报成已擦**。删除失败（EIO/ENOSPC/坏的
    littlefs 条目）必须落进 `failed` —— 否则串口与回执会声称凭据已清，而设备
    带着旧 Wi-Fi/旧凭据重启，服务端那边却已经解绑，现场无从判断。
    """
    report = {"erased": [], "missing": [], "failed": [], "unclassified": [],
              "protected_kept": [], "legacy_erased": [], "legacy_missing": [],
              "legacy_failed": []}
    for name in _erase_list():
        path = config.DATA_DIR + "/" + name
        if remove_if_exists(path):
            report["erased"].append(name)
        elif _exists(path):
            report["failed"].append(name)
        else:
            report["missing"].append(name)
    # 先擦 1.x 遗留，再归类 /odb2：否则下面 os.listdir 看到的还是清空前的快照。
    for path in LEGACY_ROOT_FILES:
        if remove_if_exists(path):
            report["legacy_erased"].append(path)
        elif _exists(path):
            report["legacy_failed"].append(path)
        else:
            report["legacy_missing"].append(path)
    # 注意：MicroPython 的 os 没有 os.path 子模块（ESP32 上 AttributeError，桌面 CPython 有），
    # 这里只用字符串操作。
    for name in sorted(os.listdir(config.DATA_DIR) if _exists(config.DATA_DIR) else []):
        if name.startswith(_marker_basename()):
            continue
        if name in PROTECTED_FILES:
            report["protected_kept"].append(name)
        elif name in _erase_list():
            # 走到这里说明它还躺在盘上 —— 只可能是删除失败，绝不能记成已擦。
            if name not in report["failed"]:
                report["failed"].append(name)
        else:
            report["unclassified"].append(name)
    # NVS 是**独立分区**，上面的文件擦除碰不到它。串口接入协议 v1 的 `write_key`
    # 按协议要求 2 把 key 同时写进了 NVS（`config.SERIAL_LINK_NVS_NAMESPACE`），
    # 不擦这里，`read_key` 会在文件被清后从 NVS 把**旧 key 读回来** ——
    # 真机实测过：擦除后 `read_key` 仍回旧值，"回到未绑定状态"不成立。
    # 与上面同一条规矩：擦不掉就得报出来，不能算已擦。
    report["nvs"] = nvs.erase_blob(
        config.SERIAL_LINK_NVS_NAMESPACE, config.SERIAL_LINK_NVS_KEY)
    if report["nvs"].startswith("failed"):
        report["failed"].append("nvs:" + config.SERIAL_LINK_NVS_KEY)
    if hasattr(os, "sync"):
        os.sync()
    return report


def clear_marker():
    """清标记并**核对真的清掉了**。

    只调 os.remove 不看结果是不行的：清不掉（EIO/盘满/littlefs 条目坏）时标记
    会活到下次开机，于是设备每次开机都重擦一遍——现场表现成"配好网、下次开机
    又被擦光"，而且没有任何提示。这里返回是否确认清掉，让调用方能报出来。
    """
    remove_if_exists(MARKER_FILE)
    remove_if_exists(MARKER_FILE + ".tmp")
    return not pending()


def resume_pending():
    """开机时调用：标记还在说明上次没擦完（或没来得及重启），幂等重跑并清标记。

    返回 (report, marker_cleared)。**即使有文件擦失败也会清标记** —— 留着标记
    会让下次开机把现场刚配好的网与凭据再擦一遍，那比"擦不干净"更难查。擦失败
    的文件会进 report["failed"]，由调用方打出来。
    """
    if not pending():
        return None
    report = erase_all()
    return report, clear_marker()
