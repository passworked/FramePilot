from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QProcess, QRectF, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
    QMenu,
)

from framepilot_i18n import LANGUAGE_OPTIONS, SUPPORTED_LANGUAGES, Localizer, resource_path
from steamvr_core import (
    AdaptiveRuntime,
    HardwareContext,
    PassiveVrcDataCollector,
    RuntimeConfig,
    StrategyStore,
    calculate_calibration,
    executable_dir,
    process_running,
)


APP_VERSION = "0.12.1"
STEAMVR_LAUNCH_URI = "steam://rungameid/250820"
TELEMETRY_UPLOAD_ENDPOINT = "https://round-darkness-4881.laptop7921.workers.dev"
ONBOARDING_REVISION = 5
ONBOARDING_PAGE_BUILDERS = (
    "_language_page",
    "_quality_page",
    "_target_page",
    "_welcome_page",
)
ONBOARDING_PAGE_COUNT = len(ONBOARDING_PAGE_BUILDERS)
AUTO_UPLOAD_MIN_INTERVAL_SECONDS = 15 * 60
AUTO_UPLOAD_RECORD_THRESHOLD = 25
AUTO_UPLOAD_MAX_BACKOFF_SECONDS = 2 * 60 * 60
SHOW_AB_EXPERIMENT_UI = False

OVERLAY_HOST = "127.0.0.1"
OVERLAY_PORT = 39421
OVERLAY_FIELD_OPTIONS = (
    ("fps", "实时帧率"),
    ("gpu_ms", "GPU 帧时间"),
    ("cpu_ms", "CPU 帧时间"),
    ("gpu_util", "GPU 占用率"),
    ("cpu_util", "CPU 占用率"),
    ("budget", "帧预算"),
    ("resolution", "等效分辨率"),
    ("scale", "SteamVR 比例"),
    ("decision", "调度动作"),
    ("reprojection", "重投影"),
    ("vrc_context", "VRC 世界与人数"),
)
DEFAULT_OVERLAY_FIELDS = (
    "fps",
    "gpu_ms",
    "cpu_ms",
    "gpu_util",
    "budget",
    "resolution",
    "scale",
    "decision",
    "vrc_context",
)


def setting_bool(settings: QSettings, key: str, default: bool = False) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def setting_number(settings: QSettings, key: str, default: float = 0.0) -> float:
    try:
        return float(settings.value(key, default))
    except (TypeError, ValueError):
        return float(default)


def request_steamvr_start(
    process_checker: Callable[[str], bool] = process_running,
    url_opener: Callable[[str], object] | None = None,
) -> tuple[str, str]:
    """Request SteamVR startup through Steam without assuming an install path."""
    if process_checker("vrserver.exe"):
        return "already_running", ""
    if url_opener is None:
        url_opener = getattr(os, "startfile", None)
    if url_opener is None:
        return "unsupported", "Steam URL launching is unavailable on this platform"
    try:
        url_opener(STEAMVR_LAUNCH_URI)
    except OSError as exc:
        return "failed", str(exc)
    return "requested", ""


def auto_upload_due(
    records: int,
    uploaded_records: int,
    now: float,
    last_attempt_at: float,
    next_allowed_at: float,
    *,
    force: bool = False,
) -> tuple[bool, float]:
    pending = max(0, int(records) - int(uploaded_records))
    if pending <= 0:
        return False, 0.0
    if now < next_allowed_at:
        return False, next_allowed_at
    interval_due_at = (
        last_attempt_at + AUTO_UPLOAD_MIN_INTERVAL_SECONDS
        if last_attempt_at > 0.0
        else now
    )
    if force or pending >= AUTO_UPLOAD_RECORD_THRESHOLD or now >= interval_due_at:
        return True, now
    return False, interval_due_at


def cached_write_permission(settings: QSettings) -> bool:
    """Restore the user's explicit SteamVR resolution-control choice."""
    return setting_bool(settings, "runtime/armed", False)


def compare_ab_results(results: list[dict[str, object]]) -> dict[str, object] | None:
    a_results = [item for item in results if item.get("variant") == "A"][-3:]
    b_results = [item for item in results if item.get("variant") == "B"][-3:]
    if len(a_results) < 3 or len(b_results) < 3:
        return None
    a_peak = sum(float(item["adjustment_peak_ms"]) for item in a_results) / 3.0
    b_peak = sum(float(item["adjustment_peak_ms"]) for item in b_results) / 3.0
    startup_ok = max(float(item["startup_over_2x_seconds"]) for item in b_results) <= 1.0
    qualified = startup_ok and a_peak > 0.0 and b_peak <= a_peak * 0.70
    return {
        "qualified": qualified,
        "a_adjustment_peak_mean_ms": round(a_peak, 3),
        "b_adjustment_peak_mean_ms": round(b_peak, 3),
        "peak_reduction_pct": round((1.0 - b_peak / a_peak) * 100.0, 1) if a_peak else 0.0,
        "startup_ok": startup_ok,
    }


ZH_EN = {
    "实时帧预算控制台 · 默认只读": "Real-time frame budget console · Read-only by default",
    "等待场景应用": "Waiting for scene application",
    "正在连接": "Connecting",
    "GPU 帧时间 P95": "GPU frame time P95",
    "CPU 帧时间 P95": "CPU frame time P95",
    "系统 GPU 占用": "System GPU usage",
    "SteamVR 分辨率": "SteamVR resolution",
    "帧预算 —": "Frame budget —",
    "系统 CPU —": "System CPU —",
    "建议 —": "Recommendation —",
    "最近 3 分钟性能趋势": "Performance trend · Last 3 minutes",
    "事件与写入记录": "Events and write history",
    "清空": "Clear",
    "运行模式": "Run mode",
    "控制预设": "Control preset",
    "执行方式": "Execution mode",
    "目标帧率预算": "Target frame-rate budget",
    "自动（跟随头显）": "Auto (follow HMD)",
    "原生刷新率": "Native refresh",
    "刷新率的 1/2": "1/2 refresh",
    "刷新率的 1/3": "1/3 refresh",
    "刷新率的 1/4": "1/4 refresh",
    "高级模式": "Advanced mode",
    "仅改变动态分辨率预算，不是游戏限帧器": "Changes the dynamic-resolution budget; not a game FPS limiter",
    "只读监控": "Read-only monitor",
    "单步自动调整": "One-step adjustment",
    "连续自适应": "Continuous adaptive",
    "允许 FramePilot VR 调控 SteamVR 分辨率": "Allow FramePilot VR to control SteamVR resolution",
    "启动 FramePilot VR 时自动启动 SteamVR": "Start SteamVR automatically with FramePilot VR",
    "退出时恢复启动值": "Restore startup value on exit",
    "分辨率调节范围与规则": "Resolution adjustment range and rules",
    "程序只自动改变分辨率，不会自行修改这些规则。": "The app changes only resolution; it does not rewrite these rules.",
    "最低": "Minimum",
    "最高": "Maximum",
    "降档步长": "Step down",
    "升档步长": "Step up",
    "升档冷却": "Raise cooldown",
    "升档后保护": "Post-raise protection",
    "回退后禁升": "Raise lockout after rollback",
    "升档 GPU 上限": "Raise GPU limit",
    "冷却": "Cooldown",
    "升档观察": "Raise observation",
    "应用控制参数": "Apply control parameters",
    "跨机器策略与本机校准": "Portable policy and local calibration",
    "等待读取本机 GPU / HMD 指纹": "Waiting for local GPU / HMD fingerprint",
    "导入策略": "Import policy",
    "导出策略": "Export policy",
    "采样时长": "Sample duration",
    "只读校准": "Read-only calibration",
    "精确阶梯": "Precise steps",
    "便携策略不包含另一台机器的最终分辨率": "Portable policies never include another PC's final resolution",
    "手动验证": "Manual validation",
    "目标分辨率": "Target resolution",
    "应用一次": "Apply once",
    "恢复面板启动值": "Restore panel startup value",
    "等待性能数据": "Waiting for performance data",
    "保守": "Conservative",
    "平衡": "Balanced",
    "激进": "Aggressive",
    "自定义/已迁移": "Custom / imported",
    "显示面板": "Show panel",
    "退出": "Exit",
    "参数错误": "Invalid parameters",
    "匿名负载采集": "Anonymous load collection",
    "正常游玩时自动采集（仅保存在本机）": "Collect automatically during normal play (local only)",
    "自动上传尚未上传的匿名记录": "Automatically upload pending anonymous records",
    "自动过滤加载期并汇总稳定负载与人数变化；不会控制 VRChat。自动上传可在首次使用引导或此处随时取消。": "Filters loading periods and aggregates stable load and population changes without controlling VRChat. Automatic upload can be disabled in the first-use guide or here at any time.",
    "等待有效采集记录": "Waiting for valid records",
    "导出共享数据": "Export sharing data",
    "上传共享数据": "Upload sharing data",
    "实时帧率": "Real-time frame rate",
    "GPU 帧时间": "GPU frame time",
    "CPU 帧时间": "CPU frame time",
    "GPU 占用率": "GPU usage",
    "CPU 占用率": "CPU usage",
    "帧预算": "Frame budget",
    "等效分辨率": "Equivalent resolution",
    "SteamVR 比例": "SteamVR scale",
    "调度动作": "Scheduler action",
    "重投影": "Reprojection",
    "VRC 世界与人数": "VRC world and population",
    "首次使用引导": "First-use guide",
    "使用引导": "Guide",
    "上一步": "Back",
    "下一步": "Next",
    "开始使用": "Get started",
    "选择你的语言": "Choose Your Language",
    "选择最适合你的显示语言。之后可以随时在主面板中更改。": "Choose the display language that works best for you. You can change it later in the main panel.",
    "选择语言后，界面会立即切换。": "The interface updates immediately when you select a language.",
    "请先把串流画质调到最高档": "Set streaming quality to its highest preset first",
    "动态分辨率需要以串流软件提供的最高基础画质为起点，否则面板只能在一个较低的编码分辨率上继续缩放。": "Dynamic resolution should start from the highest base quality offered by your streaming software; otherwise the panel can only scale a lower encoded resolution.",
    "<b>PICO 互联：</b>选择“超高清+”<br><br><b>Virtual Desktop：</b>选择“Monster”<br><br>其他串流软件请选择设备与显卡能够使用的最高画质档。": "<b>PICO Connect:</b> choose “Ultra HD+”<br><br><b>Virtual Desktop:</b> choose “Monster”<br><br>For other streaming software, choose the highest quality preset supported by your device and GPU.",
    "我已在串流软件中选择最高档画质": "I selected the highest streaming-quality preset",
    "选择目标帧率": "Choose a target frame rate",
    "这里选择的是动态分辨率使用的帧预算，不会替游戏安装限帧器。": "This selects the frame budget used by dynamic resolution; it does not install a frame limiter in the game.",
    "原生刷新率 · 画面最流畅，性能要求最高": "Native refresh · Smoothest motion, highest performance demand",
    "刷新率的 1/2 · 推荐从这里开始": "1/2 refresh · Recommended starting point",
    "刷新率的 1/3 · 适合约 30 FPS 的高画质目标": "1/3 refresh · Suited to a high-quality target near 30 FPS",
    "刷新率的 1/4 · 优先画质与重型场景": "1/4 refresh · Prioritizes quality and demanding scenes",
    "不确定时选择 1/2；之后可以随时在主面板中修改。": "Choose 1/2 if unsure; you can change it later in the main panel.",
    "欢迎使用 FramePilot VR": "Welcome to FramePilot VR",
    "设置即将完成。是否允许 FramePilot VR 调控 SteamVR 分辨率，请由你手动选择。": "Setup is almost complete. Choose whether FramePilot VR may control SteamVR resolution.",
    "自动上传匿名采集数据（推荐）": "Automatically upload anonymous collection data (recommended)",
    "勾选后，现有及以后生成的尚未上传聚合记录会自动增量上传；可随时取消。包含世界 ID、人数范围、硬件型号、渲染设置和聚合性能指标；不包含玩家身份、实例 ID、机器名或硬件指纹。": "When enabled, existing and future pending aggregate records are uploaded incrementally; you can disable this at any time. Data includes world ID, population range, hardware model, render settings, and aggregate performance metrics; it excludes player identity, instance ID, machine name, and hardware fingerprint.",
    "允许 FramePilot VR 调控 SteamVR 分辨率（保存此选择）": "Allow FramePilot VR to control SteamVR resolution (save this choice)",
    "勾选后，单步、连续和手动操作可以修改当前游戏的 SteamVR resolutionScale。此选择会保存在本机，直到你主动取消。": "When enabled, one-step, continuous, and manual actions may change the current game's SteamVR resolutionScale. This choice is saved on this PC until you disable it.",
    "第 {current} 步，共 {total} 步": "Step {current} of {total}",
    "自动增量上传": "Automatic incremental upload",
    "仅保存在本机": "Keep on this PC only",
    "串流画质：已确认最高档": "Streaming quality: highest preset confirmed",
    "目标帧率：{target}": "Target frame rate: {target}",
    "匿名采集数据：{mode}": "Anonymous collection data: {mode}",
    "SteamVR 分辨率控制：{mode}": "SteamVR resolution control: {mode}",
    "已允许并保存": "Allowed and saved",
    "保持锁定": "Locked",
    "自动上传已启用": "Automatic upload enabled",
    "仅本地采集": "Local collection only",
    "首次使用引导完成": "First-use guide completed",
    "等效分辨率（单眼）": "Equivalent resolution (per eye)",
    "VR 参数叠加层": "VR metrics overlay",
    "在头显中显示参数": "Show metrics in the headset",
    "未启用": "Disabled",
    "透明 OSD · 无图表 · 勾选需要显示的参数": "Transparent OSD · No charts · Select metrics to display",
    "左上": "Upper left",
    "右上": "Upper right",
    "左下": "Lower left",
    "右下": "Lower right",
    "头显位置": "Headset position",
    "显示大小": "Display size",
    "正在启动": "Starting",
    "等待 SteamVR": "Waiting for SteamVR",
    "等待场景": "Waiting for scene",
    "正常显示": "Active",
    "错误": "Error",
    "正在上传…": "Uploading…",
    "{minutes} 分钟后重试": "Retry in {minutes} min",
    "采集中": "Collecting",
    "已暂停": "Paused",
    "{prefix} · 世界 {worlds}/30 · 世界/人数场景 {contexts}\n稳定窗口 {steady} · 人数变化 {transitions} · {size_kib:.1f} KiB": "{prefix} · {worlds}/30 worlds · {contexts} world/population contexts\n{steady} steady windows · {transitions} population transitions · {size_kib:.1f} KiB",
    "导出匿名共享数据": "Export anonymous sharing data",
    "ZIP 压缩包 (*.zip)": "ZIP archive (*.zip)",
    "导出失败": "Export failed",
    "导出完成": "Export complete",
    "上传暂缓": "Upload delayed",
    "上传失败": "Upload failed",
    "上传完成": "Upload complete",
    "导出便携策略": "Export portable policy",
    "JSON 策略 (*.json)": "JSON policy (*.json)",
    "导入便携策略": "Import portable policy",
    "等待数据": "Waiting for data",
    "写入锁定": "Writes locked",
    "开始精确阶梯校准": "Start precise stepped calibration",
    "允许修改分辨率": "Allow resolution changes",
    "应用分辨率": "Apply resolution",
    "校准失败": "Calibration failed",
    "需要先进入 VR 游戏并取得稳定帧时序。": "Enter a VR game and wait for stable frame timings first.",
    "精确阶梯校准需要先允许 FramePilot VR 调控 SteamVR 分辨率。": "Allow FramePilot VR to control SteamVR resolution before precise stepped calibration.",
    "校准会短暂测试当前值、-10% 和 +10%，结束后自动恢复原值。请保持在同一代表性场景。": "Calibration briefly tests the current value, -10%, and +10%, then restores the original value. Keep the same representative scene.",
    "正在校准；请保持游戏场景与视角尽量稳定": "Calibrating; keep the game scene and view as stable as possible",
    "允许后，单步、连续和手动模式都可以修改当前游戏的 SteamVR resolutionScale。\n\n此选择会保存在本机，直到你主动取消。建议先使用只读模式观察，再进行单步验证。": "When allowed, one-step, continuous, and manual modes may change the current game's SteamVR resolutionScale.\n\nThis choice is saved on this PC until you disable it. Observe in read-only mode first, then validate one step.",
    "请先勾选“允许 FramePilot VR 调控 SteamVR 分辨率”。": "Enable “Allow FramePilot VR to control SteamVR resolution” first.",
    "只读监控 · 不会修改 SteamVR": "READ-ONLY · SteamVR will not be modified",
    "连续控制已启用 · 分辨率可能持续变化": "LIVE CONTROL ENABLED · Resolution may change continuously",
    "已允许写入 · 单步和手动操作可能修改分辨率": "WRITES ALLOWED · One-step and manual actions may change resolution",
    "当前设置低于配置下限": "Current setting is below the configured minimum",
    "当前设置高于配置上限": "Current setting is above the configured maximum",
    "GPU/交付压力高，但已到分辨率下限": "GPU/delivery pressure is high, but resolution is already at minimum",
    "GPU 帧时间或重投影超过安全阈值": "GPU frame time or reprojection exceeded the safety threshold",
    "CPU 受限；降低分辨率通常无效": "CPU-bound; lowering resolution is usually ineffective",
    "性能余量充足，但已到分辨率上限": "Performance headroom is available, but resolution is already at maximum",
    "处于滞回区间": "Within the hysteresis band",
    "系统 GPU 已接近满载；保留余量并禁止升档": "System GPU is near saturation; preserving headroom and blocking resolution increases",
    "当前没有 VR 场景应用，等待游戏提交画面": "No VR scene application; waiting for the game to submit frames",
    "尚未取得帧时序；等待场景应用提交画面": "No frame timings yet; waiting for the scene application",
    "控制参数已更新": "Control parameters updated",
    "SteamVR 未运行，面板将自动重连": "SteamVR is not running; the panel will reconnect automatically",
    "SteamVR 已连接": "SteamVR connected",
    "连接已断开": "Disconnected",
    "校准期间场景应用发生变化，已取消": "Scene application changed during calibration; calibration cancelled",
    "写入锁尚未解锁": "Setting writes are still locked",
    "当前没有场景应用": "No current scene application",
    "没有可恢复的启动值": "No startup value is available to restore",
    "性能余量已稳定": "Performance headroom stable for",
    "性能余量观察中": "Observing performance headroom",
    "GPU 余量不足以回升": "Insufficient GPU headroom to raise",
    "CPU 波动超过回升线": "CPU variation exceeds the raise threshold",
    "检测到重投影": "Reprojection detected",
    "暂缓回升": "raise deferred",
    "等待形成连续稳定余量": "Waiting for continuous stable headroom",
    "单次升档限制为": "Per-raise step limited to",
    "已连接 SteamVR": "Connected to SteamVR",
    "推荐目标": "recommended target",
    "硬件配置": "Hardware profile",
    "场景应用": "Scene application",
    "显式设置": "explicit setting",
    "默认值": "default",
    "检测到外部设置变化": "External setting change detected",
    "开始精确阶梯校准": "Started precise stepped calibration",
    "开始只读校准": "Started read-only calibration",
    "个阶段": "stages",
    "校准完成": "Calibration complete",
    "建议": "recommended",
    "检测为 CPU 受限": "CPU-bound detected",
    "校准阶段切换到": "Calibration stage changed to",
    "取消校准时恢复失败": "Failed to restore while cancelling calibration",
    "无法开始校准": "Unable to start calibration",
    "阶段": "Stage",
    "日志已创建": "Log created",
    "SteamVR 采样失败": "SteamVR sampling failed",
    "已恢复": "restored to",
    "恢复": "Restore",
    "失败": "failed",
    "手动应用": "manual apply",
    "自动应用": "Automatically applied",
    "截图已保存": "Screenshot saved",
    "便携策略已导出": "Portable policy exported",
    "已导入便携策略": "Portable policy imported",
    "读取本机校准失败": "Failed to read local calibration",
    "秒": "s",
    "旧策略自定义": "Legacy custom",
    "原生": "native",
    "旧策略": "legacy",
    "目标": "target",
    "基准": "base",
    "系统 CPU": "System CPU",
    "写入": "writes",
    "当前建议": "Current recommendation",
    "语言已切换": "Language switched",
    "SteamVR 已在运行，已跳过自动启动": "SteamVR is already running; automatic startup was skipped",
    "已请求 Steam 启动 SteamVR": "Requested Steam to start SteamVR",
    "无法自动启动 SteamVR": "Unable to start SteamVR automatically",
    "保存此选项；从下次启动 FramePilot VR 起生效。": "Save this option; it takes effect the next time FramePilot VR starts.",
    "未勾选时只监控和推荐，不会写入 SteamVR。勾选后，单步、连续和手动操作可修改当前游戏的 resolutionScale；此选择会保存到本机。": "When cleared, the app only monitors and recommends without writing to SteamVR. When selected, one-step, continuous, and manual actions may change the current game's resolutionScale; this choice is saved on this PC.",
    "导入失败": "Import failed",
    "共享数据正在上传": "Sharing data is already uploading",
    "VR 参数叠加层已启动；SteamVR 未运行时会自动等待": "VR metrics overlay started; it will wait automatically when SteamVR is not running",
    "VR 叠加层异常退出，代码": "VR overlay exited unexpectedly, code",
    "VR 叠加层启动失败": "VR overlay failed to start",
    "匿名共享数据已导出": "Anonymous sharing data exported",
    "可供后期上传的压缩包已保存到：\n{path}\n\n其中不包含玩家身份、实例 ID、机器名或硬件指纹。": "Upload-ready archive saved to:\n{path}\n\nNo player identity, instance ID, machine name, or hardware fingerprint is included.",
    "服务器要求暂缓上传，请在约 {minutes} 分钟后重试。本地记录仍安全保留，没有被删除。": "The server asked this client to wait. Try again in {minutes} minute(s). Local records are safe and have not been deleted.",
    "是否把尚未上传的本地记录发送到 FramePilot 共享服务？\n\n包含：世界 ID、人数区间与变化、GPU/HMD 型号、GPU 显存、CPU 型号与核心/线程数、系统内存、渲染设置和聚合性能指标。\n\n不包含：玩家身份、VRChat 实例 ID、机器名或硬件指纹。只有本次确认后才会上传。": "Upload all new local records to the FramePilot sharing service?\n\nIncluded: world ID, population ranges and changes, GPU/HMD model, GPU VRAM, CPU model and core/thread counts, system RAM, render settings, and aggregated performance metrics.\n\nExcluded: player identity, VRChat instance ID, machine name, and hardware fingerprint. Uploading happens only after this confirmation.",
    "开始自动上传新增匿名记录": "Started automatic upload of new anonymous records",
    "开始上传匿名共享数据": "Started anonymous sharing-data upload",
    "上传暂时受限，本地记录仍安全保留；约 {minutes} 分钟后重试。": "Upload is temporarily delayed; local records are safe. Retrying in about {minutes} minute(s).",
    "没有需要上传的新记录。": "No new records need uploading.",
    "上传完成：服务器接收 {accepted} 条，重复 {duplicates} 条，共 {batches} 个批次。": "Upload complete: {accepted} accepted, {duplicates} duplicate record(s), across {batches} batch(es).",
    "\n仍有记录未上传，请再次点击“上传共享数据”继续。": "\nSome records remain; click Upload again to continue.",
    "已导出阈值、步长和时间窗口。硬件指纹与本机最终分辨率未被导出。": "Thresholds, steps, and timing windows were exported. The hardware fingerprint and local final resolution were excluded.",
    "策略已迁移；当前保持只读，请在本机运行校准后采用分辨率范围": "Policy imported in read-only mode; run local calibration before adopting a resolution range",
    "只读估算": "Read-only estimate",
    "CPU 受限，建议未主动降分辨率": "CPU-bound; resolution was not proactively lowered",
    "场景校准完成 · {precision} · 建议 {recommended}% · 范围 {minimum}–{maximum}%{bound}": "Scene calibration complete · {precision} · recommended {recommended}% · range {minimum}–{maximum}%{bound}",
    "将当前场景应用从 {current}% 调整为 {target}%？": "Change the current scene application from {current}% to {target}%?",
    "目标 {cadence} ({fps:g} FPS)": "target {cadence} ({fps:g} FPS)",
    "帧预算 {budget:.2f} ms · {ratio:.0f}%": "Frame budget {budget:.2f} ms · {ratio:.0f}%",
    "系统 CPU {value:.0f}%": "System CPU {value:.0f}%",
    "SteamVR {scale}% · 建议 {arrow} {proposed}%": "SteamVR {scale}% · Recommended {arrow} {proposed}%",
    "{refresh:.0f} Hz · {target} · 基准 {width}×{height} · 写入 {count} 次": "{refresh:.0f} Hz · {target} · base {width}×{height} · writes {count}",
    "当前建议：{proposed}%": "Current recommendation: {proposed}%",
}
LOCALIZER = Localizer(ZH_EN)


STYLE = """
QWidget {
    color: #E8EDF4;
    font-family: "Segoe UI", "Microsoft YaHei UI", "Yu Gothic UI", "Meiryo UI", "Malgun Gothic";
    font-size: 13px;
}
QMainWindow, QWidget#Root { background: #0B1017; }
QFrame#Card, QGroupBox {
    background: #111923;
    border: 1px solid #223041;
    border-radius: 12px;
}
QGroupBox {
    margin-top: 12px;
    padding: 18px 12px 12px 12px;
    font-weight: 600;
    color: #AFC0D5;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QLabel#Muted { color: #8091A6; }
QLabel#Title { font-size: 23px; font-weight: 700; color: #F4F7FB; }
QLabel#Value { font-size: 25px; font-weight: 700; color: #FFFFFF; }
QLabel#StatusGood {
    color: #6FE0B1;
    background: #102C26;
    border: 1px solid #235C4D;
    border-radius: 10px;
    padding: 5px 11px;
    font-weight: 600;
}
QLabel#StatusWait {
    color: #F2C36B;
    background: #2A2415;
    border: 1px solid #5A4A22;
    border-radius: 10px;
    padding: 5px 11px;
    font-weight: 600;
}
QPushButton {
    background: #1B2A3A;
    border: 1px solid #2D435A;
    border-radius: 8px;
    padding: 8px 12px;
    font-weight: 600;
}
QPushButton:hover { background: #24384D; border-color: #3A5874; }
QPushButton:pressed { background: #152434; }
QPushButton#Primary { background: #1677FF; border-color: #3A8CFF; color: white; }
QPushButton#Primary:hover { background: #2D85FF; }
QPushButton#Danger { background: #3A1B22; border-color: #71303D; color: #FFB3C0; }
QPushButton:disabled { background: #151C25; border-color: #222C38; color: #5E6A78; }
QComboBox, QSpinBox, QDoubleSpinBox {
    background: #0D141D;
    border: 1px solid #2A3A4D;
    border-radius: 7px;
    padding: 6px 8px;
    min-height: 22px;
}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #3689F7; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QTextEdit {
    background: #090E14;
    border: 1px solid #1D2936;
    border-radius: 9px;
    padding: 7px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
}
QProgressBar {
    background: #0D141D;
    border: 1px solid #2A3A4D;
    border-radius: 6px;
    min-height: 12px;
    text-align: center;
    color: #DCE5EF;
}
QProgressBar::chunk { background: #1677FF; border-radius: 5px; }
QScrollBar:vertical { background: #0B1119; width: 10px; border: none; }
QScrollBar::handle:vertical { background: #2A3B4D; min-height: 30px; border-radius: 5px; }
QToolTip { background: #182331; color: #F3F6FA; border: 1px solid #34495F; padding: 5px; }
"""


PRESETS = {
    "保守": dict(
        min_scale=40,
        max_scale=120,
        step_down=1,
        step_up=5,
        cooldown_seconds=15.0,
        raise_stable_seconds=20.0,
        gpu_down_ratio=0.97,
        gpu_raise_ratio=0.62,
    ),
    "平衡": dict(
        min_scale=30,
        max_scale=150,
        step_down=1,
        step_up=5,
        cooldown_seconds=8.0,
        raise_stable_seconds=12.0,
        gpu_down_ratio=0.92,
        gpu_raise_ratio=0.72,
    ),
    "激进": dict(
        min_scale=20,
        max_scale=200,
        step_down=1,
        step_up=10,
        cooldown_seconds=4.0,
        raise_stable_seconds=6.0,
        gpu_down_ratio=0.87,
        gpu_raise_ratio=0.80,
    ),
}


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", subtitle: str = "") -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumHeight(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("Muted")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("Value")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("Muted")
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.subtitle_label)

    def set_values(self, value: str, subtitle: str = "") -> None:
        self.value_label.setText(value)
        self.subtitle_label.setText(subtitle)


class PerformanceChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.gpu: deque[float] = deque(maxlen=90)
        self.cpu: deque[float] = deque(maxlen=90)
        self.budget = 11.111
        self.language = "zh"

    def add_point(self, gpu_ms: float, cpu_ms: float, budget_ms: float) -> None:
        self.gpu.append(gpu_ms)
        self.cpu.append(cpu_ms)
        self.budget = budget_ms
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(55, 24, -20, -38)

        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor("#111B27"))
        gradient.setColorAt(1, QColor("#0C131C"))
        painter.fillRect(self.rect(), gradient)

        values = list(self.gpu) + list(self.cpu) + [self.budget]
        max_value = max(16.0, max(values, default=16.0) * 1.15)

        painter.setFont(QFont("Segoe UI", 9))
        for index in range(5):
            y = rect.top() + rect.height() * index / 4
            value = max_value * (1 - index / 4)
            painter.setPen(QPen(QColor("#233142"), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor("#718399"))
            painter.drawText(QRectF(3, y - 9, 47, 18), Qt.AlignmentFlag.AlignRight, f"{value:.0f} ms")

        budget_y = rect.bottom() - (self.budget / max_value) * rect.height()
        budget_pen = QPen(QColor("#E7D36A"), 1.5, Qt.PenStyle.DashLine)
        painter.setPen(budget_pen)
        painter.drawLine(QPointF(rect.left(), budget_y), QPointF(rect.right(), budget_y))
        budget_label = LOCALIZER.translate("帧预算", self.language)
        painter.drawText(QRectF(rect.right() - 115, budget_y - 20, 110, 18), Qt.AlignmentFlag.AlignRight, budget_label)

        def draw_series(series: deque[float], color: str) -> None:
            if len(series) < 2:
                return
            points = []
            count = max(2, series.maxlen or len(series))
            start = count - len(series)
            for i, value in enumerate(series):
                x = rect.left() + rect.width() * (start + i) / (count - 1)
                y = rect.bottom() - min(value, max_value) / max_value * rect.height()
                points.append(QPointF(x, y))
            painter.setPen(QPen(QColor(color), 2.2))
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)

        draw_series(self.gpu, "#42D7FF")
        draw_series(self.cpu, "#FF9D66")

        painter.setPen(QColor("#42D7FF"))
        painter.drawText(QRectF(rect.left(), rect.bottom() + 10, 100, 20), "● GPU P95")
        painter.setPen(QColor("#FF9D66"))
        painter.drawText(QRectF(rect.left() + 105, rect.bottom() + 10, 100, 20), "● CPU P95")


class OnboardingDialog(QDialog):
    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle(self.main_window.tr("首次使用引导"))
        self.setModal(True)
        self.setMinimumSize(640, 500)
        self.resize(680, 540)
        self.setStyleSheet(
            "QDialog{background:#0B1017;}"
            "QLabel#GuideTitle{font-size:25px;font-weight:700;color:#F4F7FB;}"
            "QLabel#GuideHeroTitle{font-size:30px;font-weight:700;color:#F7F8FA;}"
            "QLabel#GuideStep{color:#8E99A8;font-weight:600;}"
            "QLabel#GuideCard{background:#111923;border:1px solid #26384C;border-radius:10px;padding:14px;}"
            "QPushButton#LanguageChoice{background:#141A22;border:1px solid #2A3442;"
            "border-radius:14px;padding:15px 18px;color:#F4F7FB;font-size:15px;"
            "font-weight:600;text-align:left;}"
            "QPushButton#LanguageChoice:hover{background:#19212B;border-color:#596779;}"
            "QPushButton#LanguageChoice:checked{background:#0A84FF;border-color:#66B2FF;color:white;}"
            "QPushButton#GuideNav{background:transparent;border:1px solid #354150;"
            "border-radius:9px;padding:9px 20px;color:#DDE4EC;font-weight:600;}"
            "QPushButton#GuideNav:hover{background:#151D27;border-color:#667486;}"
            "QPushButton#Primary{background:#0A84FF;border:1px solid #0A84FF;"
            "border-radius:9px;padding:9px 22px;color:white;font-weight:650;}"
            "QPushButton#Primary:hover{background:#2997FF;border-color:#2997FF;}"
            "QPushButton#Primary:disabled{background:#151C25;border-color:#222C38;color:#5E6A78;}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)
        self.step_label = QLabel()
        self.step_label.setObjectName("GuideStep")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.step_label)

        self.pages = QStackedWidget()
        for builder_name in ONBOARDING_PAGE_BUILDERS:
            self.pages.addWidget(getattr(self, builder_name)())
        self.pages.currentChanged.connect(self._page_changed)
        root.addWidget(self.pages, 1)

        navigation = QHBoxLayout()
        self.back_button = QPushButton(self.main_window.tr("上一步"))
        self.back_button.setObjectName("GuideNav")
        self.back_button.clicked.connect(self.previous_page)
        self.next_button = QPushButton(self.main_window.tr("下一步"))
        self.next_button.setObjectName("Primary")
        self.next_button.setMinimumWidth(104)
        self.next_button.clicked.connect(self.next_page)
        navigation.addWidget(self.back_button)
        navigation.addStretch()
        navigation.addWidget(self.next_button)
        root.addLayout(navigation)
        self._page_changed(0)

    @staticmethod
    def _title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("GuideTitle")
        label.setWordWrap(True)
        return label

    def _language_page(self) -> QWidget:
        tr = self.main_window.tr
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 28, 6)
        layout.setSpacing(12)

        self.language_title = QLabel(tr("选择你的语言"))
        self.language_title.setObjectName("GuideHeroTitle")
        self.language_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.language_title.setWordWrap(True)
        layout.addWidget(self.language_title)

        self.language_subtitle = QLabel(
            tr("选择最适合你的显示语言。之后可以随时在主面板中更改。")
        )
        self.language_subtitle.setObjectName("Muted")
        self.language_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.language_subtitle.setWordWrap(True)
        layout.addWidget(self.language_subtitle)
        layout.addSpacing(10)

        choices = QGridLayout()
        choices.setHorizontalSpacing(12)
        choices.setVerticalSpacing(12)
        self.language_button_group = QButtonGroup(self)
        self.language_button_group.setExclusive(True)
        self.language_buttons: dict[str, QPushButton] = {}
        for index, (code, native_name) in enumerate(LANGUAGE_OPTIONS):
            button = QPushButton(native_name)
            button.setObjectName("LanguageChoice")
            button.setCheckable(True)
            button.setMinimumHeight(56)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(code == self.main_window.language)
            button.toggled.connect(
                lambda checked, language=code: self._select_language(language)
                if checked
                else None
            )
            self.language_button_group.addButton(button)
            self.language_buttons[code] = button
            row, column = divmod(index, 2)
            if index == len(LANGUAGE_OPTIONS) - 1:
                choices.addWidget(button, row, 0, 1, 2)
            else:
                choices.addWidget(button, row, column)
        layout.addLayout(choices)

        self.language_hint = QLabel(tr("选择语言后，界面会立即切换。"))
        self.language_hint.setObjectName("Muted")
        self.language_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.language_hint)
        layout.addStretch()
        return page

    def _select_language(self, language: str) -> None:
        index = self.main_window.language_combo.findData(language)
        if index >= 0:
            self.main_window.language_combo.setCurrentIndex(index)

    def _quality_page(self) -> QWidget:
        tr = self.main_window.tr
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._title(tr("请先把串流画质调到最高档")))
        text = QLabel(
            tr("动态分辨率需要以串流软件提供的最高基础画质为起点，否则面板只能在一个较低的编码分辨率上继续缩放。")
        )
        text.setWordWrap(True)
        text.setObjectName("Muted")
        layout.addWidget(text)
        examples = QLabel(
            tr(
                "<b>PICO 互联：</b>选择“超高清+”<br><br>"
                "<b>Virtual Desktop：</b>选择“Monster”<br><br>"
                "其他串流软件请选择设备与显卡能够使用的最高画质档。"
            )
        )
        examples.setObjectName("GuideCard")
        examples.setWordWrap(True)
        layout.addWidget(examples)
        self.quality_confirm = QCheckBox(tr("我已在串流软件中选择最高档画质"))
        self.quality_confirm.toggled.connect(self._update_next_enabled)
        layout.addWidget(self.quality_confirm)
        layout.addStretch()
        return page

    def _target_page(self) -> QWidget:
        tr = self.main_window.tr
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._title(tr("选择目标帧率")))
        text = QLabel(tr("这里选择的是动态分辨率使用的帧预算，不会替游戏安装限帧器。"))
        text.setWordWrap(True)
        text.setObjectName("Muted")
        layout.addWidget(text)
        self.guide_target_combo = QComboBox()
        self.guide_target_combo.addItem(tr("原生刷新率 · 画面最流畅，性能要求最高"), 1)
        self.guide_target_combo.addItem(tr("刷新率的 1/2 · 推荐从这里开始"), 2)
        self.guide_target_combo.addItem(tr("刷新率的 1/3 · 适合约 30 FPS 的高画质目标"), 3)
        self.guide_target_combo.addItem(tr("刷新率的 1/4 · 优先画质与重型场景"), 4)
        current_divisor = int(self.main_window.target_fps_combo.currentData())
        current_index = self.guide_target_combo.findData(current_divisor)
        self.guide_target_combo.setCurrentIndex(max(0, current_index))
        layout.addWidget(self.guide_target_combo)
        hint = QLabel(tr("不确定时选择 1/2；之后可以随时在主面板中修改。"))
        hint.setObjectName("GuideCard")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        return page

    def _welcome_page(self) -> QWidget:
        tr = self.main_window.tr
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._title(tr("欢迎使用 FramePilot VR")))
        text = QLabel(
            tr("设置即将完成。是否允许 FramePilot VR 调控 SteamVR 分辨率，请由你手动选择。")
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.guide_summary = QLabel()
        self.guide_summary.setObjectName("GuideCard")
        self.guide_summary.setWordWrap(True)
        layout.addWidget(self.guide_summary)
        self.guide_auto_upload = QCheckBox(tr("自动上传匿名采集数据（推荐）"))
        self.guide_auto_upload.setChecked(
            setting_bool(self.main_window.settings, "collection/auto_upload", True)
            if self.main_window.settings.contains("collection/auto_upload")
            else True
        )
        self.guide_auto_upload.toggled.connect(self._refresh_summary)
        layout.addWidget(self.guide_auto_upload)
        upload_hint = QLabel(
            tr(
                "勾选后，现有及以后生成的尚未上传聚合记录会自动增量上传；可随时取消。"
                "包含世界 ID、人数范围、硬件型号、渲染设置和聚合性能指标；"
                "不包含玩家身份、实例 ID、机器名或硬件指纹。"
            )
        )
        upload_hint.setObjectName("Muted")
        upload_hint.setWordWrap(True)
        layout.addWidget(upload_hint)
        self.guide_allow_resolution = QCheckBox(
            tr("允许 FramePilot VR 调控 SteamVR 分辨率（保存此选择）")
        )
        self.guide_allow_resolution.setChecked(self.main_window.arm_check.isChecked())
        self.guide_allow_resolution.toggled.connect(self._refresh_summary)
        layout.addWidget(self.guide_allow_resolution)
        resolution_hint = QLabel(
            tr(
                "勾选后，单步、连续和手动操作可以修改当前游戏的 SteamVR resolutionScale。"
                "此选择会保存在本机，直到你主动取消。"
            )
        )
        resolution_hint.setObjectName("Muted")
        resolution_hint.setWordWrap(True)
        layout.addWidget(resolution_hint)
        layout.addStretch()
        return page

    def _update_next_enabled(self) -> None:
        self.next_button.setEnabled(
            self.pages.currentIndex() != 1 or self.quality_confirm.isChecked()
        )

    def _page_changed(self, index: int) -> None:
        self.step_label.setText(
            self.main_window.trf(
                "第 {current} 步，共 {total} 步",
                current=index + 1,
                total=ONBOARDING_PAGE_COUNT,
            )
        )
        self.back_button.setVisible(index > 0)
        self.next_button.setText(
            self.main_window.tr(
                "开始使用"
                if index == ONBOARDING_PAGE_COUNT - 1
                else "下一步"
            )
        )
        if index == ONBOARDING_PAGE_COUNT - 1:
            self._refresh_summary()
        self._update_next_enabled()

    def retranslate_ui(self) -> None:
        tr = self.main_window.tr
        self.setWindowTitle(tr("首次使用引导"))
        self.language_title.setText(tr("选择你的语言"))
        self.language_subtitle.setText(
            tr("选择最适合你的显示语言。之后可以随时在主面板中更改。")
        )
        self.language_hint.setText(tr("选择语言后，界面会立即切换。"))
        target_names = (
            "原生刷新率 · 画面最流畅，性能要求最高",
            "刷新率的 1/2 · 推荐从这里开始",
            "刷新率的 1/3 · 适合约 30 FPS 的高画质目标",
            "刷新率的 1/4 · 优先画质与重型场景",
        )
        for index, source in enumerate(target_names):
            self.guide_target_combo.setItemText(index, tr(source))
        for code, button in self.language_buttons.items():
            button.blockSignals(True)
            button.setChecked(code == self.main_window.language)
            button.blockSignals(False)
        self._page_changed(self.pages.currentIndex())

    def _refresh_summary(self) -> None:
        upload_text = self.main_window.tr(
            "自动增量上传" if self.guide_auto_upload.isChecked() else "仅保存在本机"
        )
        resolution_text = self.main_window.tr(
            "已允许并保存" if self.guide_allow_resolution.isChecked() else "保持锁定"
        )
        self.guide_summary.setText(
            f"✓ {self.main_window.tr('串流画质：已确认最高档')}<br><br>"
            f"✓ {self.main_window.trf('目标帧率：{target}', target=self.guide_target_combo.currentText())}<br><br>"
            f"✓ {self.main_window.trf('匿名采集数据：{mode}', mode=upload_text)}<br><br>"
            f"✓ {self.main_window.trf('SteamVR 分辨率控制：{mode}', mode=resolution_text)}"
        )

    def previous_page(self) -> None:
        self.pages.setCurrentIndex(max(0, self.pages.currentIndex() - 1))

    def next_page(self) -> None:
        index = self.pages.currentIndex()
        if index == 1 and not self.quality_confirm.isChecked():
            return
        if index < ONBOARDING_PAGE_COUNT - 1:
            self.pages.setCurrentIndex(index + 1)
            return
        divisor = int(self.guide_target_combo.currentData())
        target_index = self.main_window.target_fps_combo.findData(divisor)
        if target_index >= 0:
            self.main_window.target_fps_combo.setCurrentIndex(target_index)
        self.main_window.mode_combo.blockSignals(True)
        self.main_window.mode_combo.setCurrentIndex(
            self.main_window.mode_combo.findData("monitor")
        )
        self.main_window.mode_combo.blockSignals(False)
        self.main_window.arm_check.blockSignals(True)
        self.main_window.arm_check.setChecked(self.guide_allow_resolution.isChecked())
        self.main_window.arm_check.blockSignals(False)
        self.main_window.set_auto_upload_enabled(self.guide_auto_upload.isChecked())
        self.main_window.settings.setValue("onboarding/completed", True)
        self.main_window.settings.setValue("onboarding/revision", ONBOARDING_REVISION)
        self.main_window.settings.sync()
        self.main_window.apply_config()
        upload_text = self.main_window.tr(
            "自动上传已启用" if self.guide_auto_upload.isChecked() else "仅本地采集"
        )
        self.main_window.append_event(
            "success",
            f"{self.main_window.tr('首次使用引导完成')} · "
            f"{self.main_window.tr('目标')} 1/{divisor} · {upload_text}",
        )
        self.accept()


class MonitorWorker(QObject):
    snapshot = Signal(dict)
    event = Signal(str, str)
    connection = Signal(bool, str)
    hardware = Signal(dict)
    calibration_progress = Signal(dict)
    calibration_finished = Signal(dict)
    experiment_progress = Signal(dict)
    experiment_finished = Signal(dict)
    collection_status = Signal(dict)
    collection_exported = Signal(dict)
    collection_uploaded = Signal(dict)
    finished = Signal()

    def __init__(self, log_dir: Path, collection_enabled: bool = True) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.runtime: AdaptiveRuntime | None = None
        local_root = Path(os.environ.get("LOCALAPPDATA", str(executable_dir()))) / "SteamVRAdaptiveResolution"
        self.strategy_store = StrategyStore(local_root / "strategy-store.json")
        self.passive_collector = PassiveVrcDataCollector(
            local_root / "shared-telemetry", enabled=collection_enabled
        )
        self._last_collection_status_at = -1e9
        self._collection_upload_active = False
        self._collection_upload_lock = threading.Lock()
        self.calibration: dict[str, object] | None = None
        self.experiment: dict[str, object] | None = None
        self.experiment_results: list[dict[str, object]] = []
        self.experiment_counts = {"A": 0, "B": 0}
        self._last_snapshot = None
        self._hardware_emitted = ""
        self.overlay_fields = list(DEFAULT_OVERLAY_FIELDS)
        self.overlay_config = {"anchor": "upper_left", "size_pct": 100}
        self.overlay_language = "zh"
        self.overlay_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def submit_config(self, config: RuntimeConfig) -> None:
        self.commands.put(("config", config))

    def submit_manual_scale(self, scale: int) -> None:
        self.commands.put(("manual", scale))

    def submit_restore(self) -> None:
        self.commands.put(("restore", None))

    def submit_collection_enabled(self, enabled: bool) -> None:
        self.commands.put(("collection_enabled", bool(enabled)))

    def submit_collection_export(self, path: Path) -> None:
        self.commands.put(("collection_export", path))

    def submit_collection_upload(self, automatic: bool = False) -> None:
        self.commands.put(("collection_upload", bool(automatic)))

    def submit_overlay_settings(
        self,
        fields: list[str],
        anchor: str,
        size_pct: int,
        language: str,
    ) -> None:
        self.commands.put(
            (
                "overlay_settings",
                {
                    "fields": list(fields),
                    "anchor": anchor,
                    "size_pct": int(size_pct),
                    "language": language,
                },
            )
        )

    def _send_overlay_packet(self, data: dict[str, object] | None = None) -> None:
        packet = json.dumps(
            {
                "data": data,
                "visible_fields": self.overlay_fields,
                "config": self.overlay_config,
                "language": self.overlay_language,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.overlay_socket.sendto(packet, (OVERLAY_HOST, OVERLAY_PORT))
        except OSError:
            pass

    def submit_calibration(self, precise: bool, duration: float) -> None:
        self.commands.put(("calibrate", {"precise": precise, "duration": duration}))

    def submit_experiment(self, variant: str) -> None:
        self.commands.put(("experiment", variant))

    def stop(self) -> None:
        self.stop_event.set()

    def _drain_commands(self) -> None:
        assert self.runtime is not None
        while True:
            try:
                command, payload = self.commands.get_nowait()
            except queue.Empty:
                break
            try:
                if command == "config":
                    self.runtime.update_config(payload)  # type: ignore[arg-type]
                    self.event.emit("info", "控制参数已更新")
                elif command == "manual":
                    self.runtime.manual_set_scale(int(payload))
                elif command == "restore":
                    self.runtime.restore_current()
                elif command == "collection_enabled":
                    self.passive_collector.set_enabled(bool(payload))
                    self.collection_status.emit(self.passive_collector.status())
                elif command == "collection_export":
                    exported_path = self.passive_collector.export_share_package(Path(payload))
                    self.collection_exported.emit(
                        {"ok": True, "path": str(exported_path)}
                    )
                elif command == "collection_upload":
                    automatic = bool(payload)
                    with self._collection_upload_lock:
                        if self._collection_upload_active:
                            raise RuntimeError("共享数据正在上传")
                        self._collection_upload_active = True
                    try:
                        threading.Thread(
                            target=self._run_collection_upload,
                            args=(automatic,),
                            name="framepilot-telemetry-upload",
                            daemon=True,
                        ).start()
                    except Exception:
                        with self._collection_upload_lock:
                            self._collection_upload_active = False
                        raise
                elif command == "overlay_settings":
                    allowed = {field for field, _label in OVERLAY_FIELD_OPTIONS}
                    options = payload if isinstance(payload, dict) else {}
                    fields = options.get("fields", [])
                    self.overlay_fields = [str(field) for field in fields if str(field) in allowed] if isinstance(fields, list) else []
                    anchor = str(options.get("anchor", "upper_left"))
                    if anchor not in {"upper_left", "upper_right", "lower_left", "lower_right"}:
                        anchor = "upper_left"
                    self.overlay_config = {
                        "anchor": anchor,
                        "size_pct": max(70, min(140, int(options.get("size_pct", 100)))),
                    }
                    language = str(options.get("language", "zh"))
                    self.overlay_language = (
                        language if language in SUPPORTED_LANGUAGES else "zh"
                    )
                    self._send_overlay_packet()
                elif command == "calibrate":
                    self._start_calibration(payload)  # type: ignore[arg-type]
                elif command == "experiment":
                    self._start_experiment(str(payload))
            except Exception as exc:
                self.event.emit("error", str(exc))
                if command == "calibrate":
                    self.calibration_progress.emit(
                        {"percent": 0, "text": f"无法开始校准: {exc}", "done": True}
                    )
                elif command == "experiment":
                    self.experiment_finished.emit({"error": str(exc)})
                elif command == "collection_export":
                    self.collection_exported.emit({"ok": False, "error": str(exc)})
                elif command == "collection_upload":
                    self.collection_uploaded.emit(
                        {
                            "ok": False,
                            "error": str(exc),
                            "automatic": bool(payload),
                        }
                    )

    def _run_collection_upload(self, automatic: bool) -> None:
        try:
            result = self.passive_collector.upload_pending(TELEMETRY_UPLOAD_ENDPOINT)
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc),
                "http_status": int(getattr(exc, "http_status", 0)),
                "retry_after_seconds": int(
                    getattr(exc, "retry_after_seconds", 0)
                ),
            }
        finally:
            with self._collection_upload_lock:
                self._collection_upload_active = False
        result["automatic"] = automatic
        self.collection_uploaded.emit(result)

    def _start_experiment(self, variant: str) -> None:
        assert self.runtime is not None
        if variant not in {"A", "B"}:
            raise ValueError("未知 A/B 测试组")
        if self.experiment is not None:
            raise RuntimeError("已有 A/B 测试正在运行")
        if self.calibration is not None:
            raise RuntimeError("校准期间不能开始 A/B 测试")
        if self._last_snapshot is None or not self.runtime.current_app:
            raise RuntimeError("需要先进入 VRChat 场景并取得帧时序")
        if not self.runtime.config.armed:
            raise PermissionError("A/B 测试需要先解锁 SteamVR 写入")
        original_config = self.runtime.config
        original_scale = self.runtime.current_scale
        start_scale = 100 if variant == "A" else 150
        start_scale = max(original_config.min_scale, min(max(original_config.max_scale, 150), start_scale))
        trial_config = RuntimeConfig(
            **{
                **original_config.__dict__,
                "mode": "continuous",
                "armed": True,
                "target_divisor": 0,
                "target_fps": 30.0,
                "max_scale": max(original_config.max_scale, 150),
                "step_down": 1,
                "evaluate_seconds": 0.25,
                "raise_stable_seconds": min(original_config.raise_stable_seconds, 8.0),
                "startup_scale": 0,
            }
        ).validated()
        self.runtime.update_config(trial_config)
        self.runtime.experiment_set_scale(start_scale, f"A/B {variant} 组起始值")
        now = time.monotonic()
        self.experiment = {
            "variant": variant,
            "run": self.experiment_counts[variant] + 1,
            "started": now,
            "last_sample": now,
            "duration": 30.0,
            "original_config": original_config,
            "original_scale": original_scale,
            "app_key": self.runtime.current_app,
            "start_scale": start_scale,
            "write_times": [],
            "startup_over_2x_seconds": 0.0,
            "adjustment_peak_ms": 0.0,
            "overall_peak_ms": 0.0,
            "reprojection_peak_pct": 0.0,
            "dropped_peak": 0,
        }
        self.event.emit("info", f"A/B {variant} 组第 {self.experiment_counts[variant] + 1} 轮开始 · 30 FPS · 起始 {start_scale}%")
        self.experiment_progress.emit({"variant": variant, "run": self.experiment_counts[variant] + 1, "percent": 0})

    def _process_experiment(self, snapshot) -> None:
        if self.experiment is None:
            return
        state = self.experiment
        if snapshot.app_key != state["app_key"]:
            self._cancel_experiment("场景应用发生变化")
            return
        now = time.monotonic()
        started = float(state["started"])
        elapsed = now - started
        delta = max(0.0, min(1.0, now - float(state["last_sample"])))
        state["last_sample"] = now
        frame_ms = float(snapshot.frame_interval_p95_ms)
        budget_ms = float(snapshot.budget_ms)
        state["overall_peak_ms"] = max(float(state["overall_peak_ms"]), frame_ms)
        state["reprojection_peak_pct"] = max(float(state["reprojection_peak_pct"]), float(snapshot.reprojection_pct))
        state["dropped_peak"] = max(int(state["dropped_peak"]), int(snapshot.dropped))
        if elapsed <= 5.0 and frame_ms > budget_ms * 2.0:
            state["startup_over_2x_seconds"] = float(state["startup_over_2x_seconds"]) + delta
        write_times = state["write_times"]
        if snapshot.write_applied:
            write_times.append(now)
        if any(0.0 <= now - float(write_at) <= 5.0 for write_at in write_times):
            state["adjustment_peak_ms"] = max(float(state["adjustment_peak_ms"]), frame_ms)
        percent = min(100, int(elapsed / float(state["duration"]) * 100))
        self.experiment_progress.emit(
            {"variant": state["variant"], "run": state["run"], "percent": percent, "elapsed": elapsed}
        )
        if elapsed >= float(state["duration"]):
            self._finish_experiment()

    def _finish_experiment(self) -> None:
        assert self.runtime is not None and self.experiment is not None
        state = self.experiment
        variant = str(state["variant"])
        result = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "variant": variant,
            "run": int(state["run"]),
            "start_scale": int(state["start_scale"]),
            "startup_over_2x_seconds": round(float(state["startup_over_2x_seconds"]), 3),
            "adjustment_peak_ms": round(float(state["adjustment_peak_ms"]), 3),
            "overall_peak_ms": round(float(state["overall_peak_ms"]), 3),
            "reprojection_peak_pct": round(float(state["reprojection_peak_pct"]), 3),
            "dropped_peak": int(state["dropped_peak"]),
        }
        try:
            self.runtime.experiment_set_scale(int(state["original_scale"]), "A/B 测试结束恢复")
        finally:
            self.runtime.update_config(state["original_config"])  # type: ignore[arg-type]
        self.experiment_results.append(result)
        self.experiment_counts[variant] += 1
        result_path = self.log_dir / "ab_experiment_results.jsonl"
        with result_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
        result["counts"] = dict(self.experiment_counts)
        comparison = compare_ab_results(self.experiment_results)
        if comparison is not None:
            result.update(comparison)
        self.event.emit("success", f"A/B {variant} 组第 {state['run']} 轮完成 · 调档峰值 {result['adjustment_peak_ms']:.2f} ms")
        self.experiment = None
        self.experiment_progress.emit({"variant": variant, "run": state["run"], "percent": 100, "done": True})
        self.experiment_finished.emit(result)

    def _cancel_experiment(self, reason: str) -> None:
        if self.runtime is None or self.experiment is None:
            return
        state = self.experiment
        try:
            self.runtime.experiment_set_scale(int(state["original_scale"]), "A/B 测试取消恢复")
        except Exception as exc:
            self.event.emit("error", f"A/B 测试恢复失败: {exc}")
        self.runtime.update_config(state["original_config"])  # type: ignore[arg-type]
        self.experiment = None
        self.event.emit("warning", f"A/B 测试已取消: {reason}")
        self.experiment_finished.emit({"error": reason})

    def _start_calibration(self, options: dict[str, object]) -> None:
        assert self.runtime is not None
        if self.calibration is not None:
            raise RuntimeError("已有校准正在进行")
        if self.experiment is not None:
            raise RuntimeError("A/B 测试期间不能开始校准")
        if self._last_snapshot is None or not self.runtime.current_app:
            raise RuntimeError("尚未取得 VR 游戏帧时序，暂时不能校准")
        precise = bool(options.get("precise", False))
        if precise and not self.runtime.config.armed:
            raise PermissionError("精确校准需要先允许 FramePilot VR 调控 SteamVR 分辨率")
        original = int(self.runtime.current_scale)
        stages = [original]
        if precise:
            stages = list(dict.fromkeys((original, max(20, original - 10), min(500, original + 10))))
        duration = max(15.0, float(options.get("duration", 45.0)))
        previous = self.runtime.config
        self.runtime.update_config(RuntimeConfig(**{**previous.__dict__, "mode": "monitor"}))
        self.calibration = {
            "precise": precise,
            "app_key": self.runtime.current_app,
            "original": original,
            "stages": stages,
            "stage_index": 0,
            "stage_seconds": duration / len(stages),
            "stage_started": time.monotonic(),
            "started": time.monotonic(),
            "samples": {scale: [] for scale in stages},
            "budget_ms": float(self._last_snapshot.budget_ms),
            "previous_config": previous,
        }
        self.event.emit("info", f"开始{'精确阶梯' if precise else '只读'}校准 · {len(stages)} 个阶段")
        self.calibration_progress.emit({"percent": 0, "text": f"阶段 1/{len(stages)} · {stages[0]}%"})

    def _finish_calibration(self) -> None:
        assert self.runtime is not None and self.calibration is not None
        state = self.calibration
        try:
            original = int(state["original"])
            if self.runtime.current_scale != original:
                self.runtime.manual_set_scale(original)
            context = self.runtime.hardware()
            result = calculate_calibration(
                context=context,
                app_key=str(state["app_key"]),
                original_scale=original,
                budget_ms=float(state["budget_ms"]),
                samples_by_scale=state["samples"],  # type: ignore[arg-type]
                precise=bool(state["precise"]),
            )
            self.strategy_store.save_calibration(context, result)
            self.calibration_finished.emit(result.as_dict())
            bound = " · 检测为 CPU 受限" if result.cpu_bound else ""
            self.event.emit("success", f"校准完成 · 建议 {result.recommended_scale}%{bound}")
        except Exception as exc:
            self.event.emit("error", f"校准失败: {exc}")
            self.calibration_finished.emit({"error": str(exc)})
        finally:
            previous = state["previous_config"]
            self.runtime.update_config(previous)  # type: ignore[arg-type]
            self.calibration = None

    def _process_calibration(self, snapshot) -> None:
        if self.calibration is None:
            return
        state = self.calibration
        if snapshot.app_key != state["app_key"]:
            self.event.emit("error", "校准期间场景应用发生变化，已取消")
            self._cancel_calibration()
            return
        stages = state["stages"]
        stage_index = int(state["stage_index"])
        target = int(stages[stage_index])
        elapsed = time.monotonic() - float(state["stage_started"])
        stage_seconds = float(state["stage_seconds"])
        if snapshot.resolution_scale == target and elapsed >= min(2.0, stage_seconds * 0.2):
            state["samples"][target].append(snapshot)  # type: ignore[index]
        overall = (stage_index + min(1.0, elapsed / stage_seconds)) / len(stages)
        self.calibration_progress.emit(
            {"percent": int(overall * 100), "text": f"阶段 {stage_index + 1}/{len(stages)} · {target}%"}
        )
        if elapsed < stage_seconds:
            return
        if stage_index + 1 >= len(stages):
            self._finish_calibration()
            return
        state["stage_index"] = stage_index + 1
        state["stage_started"] = time.monotonic()
        next_target = int(stages[stage_index + 1])
        self.runtime.manual_set_scale(next_target)
        self.event.emit("write", f"校准阶段切换到 {next_target}%")

    def _cancel_calibration(self) -> None:
        if self.runtime is None or self.calibration is None:
            return
        state = self.calibration
        try:
            original = int(state["original"])
            if self.runtime.current_scale != original:
                self.runtime.manual_set_scale(original)
        except Exception as exc:
            self.event.emit("error", f"取消校准时恢复失败: {exc}")
        self.runtime.update_config(state["previous_config"])  # type: ignore[arg-type]
        self.calibration = None

    def run(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"steamvr_panel_{stamp}.csv"
        log_file = log_path.open("w", newline="", encoding="utf-8-sig")
        writer = None
        self.runtime = AdaptiveRuntime(RuntimeConfig(restore_on_exit=True))
        self.event.emit("info", f"日志已创建: {log_path.name}")
        self.collection_status.emit(self.passive_collector.status())
        last_connection_message = ""
        try:
            while not self.stop_event.is_set():
                self._drain_commands()
                if not process_running("vrserver.exe"):
                    if last_connection_message != "waiting":
                        self.connection.emit(False, "等待 SteamVR")
                        self.event.emit("warning", "SteamVR 未运行，面板将自动重连")
                        last_connection_message = "waiting"
                    time.sleep(1.0)
                    continue
                try:
                    result = self.runtime.poll()
                    if last_connection_message != "connected":
                        self.connection.emit(True, "SteamVR 已连接")
                        last_connection_message = "connected"
                    for level, message in self.runtime.drain_events():
                        self.event.emit(level, message)
                    if self.runtime.connected:
                        context = self.runtime.hardware()
                        if context.hardware_id != self._hardware_emitted:
                            self.hardware.emit(context.as_dict())
                            self._hardware_emitted = context.hardware_id
                    if result is not None:
                        self._last_snapshot = result
                        self._process_calibration(result)
                        self._process_experiment(result)
                        context = self.runtime.hardware()
                        collection_changed = self.passive_collector.observe(
                            result, context, time.monotonic()
                        )
                        now = time.monotonic()
                        if collection_changed or now - self._last_collection_status_at >= 5.0:
                            self._last_collection_status_at = now
                            self.collection_status.emit(self.passive_collector.status())
                        data = result.as_dict()
                        active_experiment = self.experiment
                        data.update(
                            {
                                "experiment_variant": str(active_experiment["variant"]) if active_experiment else "",
                                "experiment_run": int(active_experiment["run"]) if active_experiment else 0,
                                "experiment_elapsed_s": round(time.monotonic() - float(active_experiment["started"]), 3) if active_experiment else 0.0,
                            }
                        )
                        self._send_overlay_packet(data)
                        self.snapshot.emit(data)
                        if writer is None:
                            writer = csv.DictWriter(log_file, fieldnames=list(data.keys()))
                            writer.writeheader()
                        writer.writerow(data)
                        log_file.flush()
                except Exception as exc:
                    self.connection.emit(False, "连接已断开")
                    self.event.emit("error", f"SteamVR 采样失败: {exc}")
                    self.runtime.disconnect()
                    last_connection_message = "error"
                    time.sleep(1.5)
                time.sleep(0.2)
        finally:
            if self.runtime is not None:
                self._cancel_experiment("面板退出")
                self._cancel_calibration()
                self.runtime.close()
                for level, message in self.runtime.drain_events():
                    self.event.emit(level, message)
            self.overlay_socket.close()
            log_file.close()
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(
        self,
        screenshot_path: Path | None = None,
        auto_close: float = 0.0,
        language_override: str | None = None,
        target_divisor_override: int | None = None,
        target_fps_override: float | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"FramePilot VR · {APP_VERSION}")
        self.resize(1240, 840)
        self.setMinimumSize(1060, 700)
        self.setWindowIcon(make_icon())
        self.last_snapshot: dict[str, object] = {}
        self.last_collection_status: dict[str, object] = {}
        self._collection_uploading = False
        self._collection_upload_is_automatic = False
        self.settings = QSettings("OpenAI-Codex", "SteamVRAdaptiveResolution")
        self._auto_upload_last_success_records = int(
            setting_number(
                self.settings,
                "collection/auto_upload_last_success_records",
                0,
            )
        )
        self._auto_upload_last_attempt_at = setting_number(
            self.settings,
            "collection/auto_upload_last_attempt_at",
            0.0,
        )
        self._auto_upload_next_allowed_at = setting_number(
            self.settings,
            "collection/auto_upload_next_allowed_at",
            0.0,
        )
        self._upload_rate_limited_until = setting_number(
            self.settings,
            "collection/upload_rate_limited_until",
            0.0,
        )
        self._auto_upload_failure_count = int(
            setting_number(
                self.settings,
                "collection/auto_upload_failure_count",
                0,
            )
        )
        self._auto_upload_retry_timer = QTimer(self)
        self._auto_upload_retry_timer.setSingleShot(True)
        self._auto_upload_retry_timer.timeout.connect(
            lambda: self._maybe_auto_upload(force=True)
        )
        saved_language = str(self.settings.value("language", "zh"))
        self.language = language_override or (
            saved_language if saved_language in SUPPORTED_LANGUAGES else "zh"
        )
        self.advanced_mode = str(self.settings.value("advanced_mode", "false")).lower() == "true"
        self._loading_controls = False
        self.legacy_target_fps = 0.0
        self.connection_state = (False, "正在连接")
        self.policy_gpu_down_ratio = float(PRESETS["平衡"]["gpu_down_ratio"])
        self.policy_gpu_raise_ratio = float(PRESETS["平衡"]["gpu_raise_ratio"])
        self.policy_cpu_raise_ratio = 0.80
        self.policy_window_seconds = 3.0
        self.policy_evaluate_seconds = 0.25
        self.high_start_qualified = str(self.settings.value("experiment/high_start_qualified", "false")).lower() == "true"
        self.screenshot_path = screenshot_path
        self._closing = False
        self.overlay_process: QProcess | None = None
        self._overlay_stdout_buffer = ""
        self._overlay_status_path = (
            Path(os.environ.get("LOCALAPPDATA", str(executable_dir())))
            / "SteamVRAdaptiveResolution"
            / f"overlay-status-{os.getpid()}.json"
        )
        self._overlay_status_contents = ""
        self._overlay_status_timer = QTimer(self)
        self._overlay_status_timer.setInterval(250)
        self._overlay_status_timer.timeout.connect(self._poll_overlay_status_file)
        self.overlay_state = "disabled"
        self._overlay_expected_stop = False
        self.onboarding_dialog: OnboardingDialog | None = None
        self._skip_next_cache = target_divisor_override is not None or target_fps_override is not None

        self._build_ui()
        self.retranslate_ui(announce=False)
        self.set_advanced_mode(self.advanced_mode, persist=False)
        self._restore_cached_config()
        self._restore_overlay_config()
        if target_divisor_override is not None:
            target_index = self.target_fps_combo.findData(int(target_divisor_override))
            if target_index >= 0:
                self.target_fps_combo.setCurrentIndex(target_index)
        elif target_fps_override is not None:
            self._set_legacy_target(float(target_fps_override))
        self._start_worker()
        self._setup_tray()
        self._sync_overlay_process()
        self._send_overlay_settings()
        QTimer.singleShot(0, self._autostart_steamvr_if_enabled)
        QTimer.singleShot(500, self.maybe_show_onboarding)

        if screenshot_path is not None:
            QTimer.singleShot(3500, self.capture_screenshot)
        if auto_close > 0:
            QTimer.singleShot(int(auto_close * 1000), self.close)
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 17, 20, 18)
        outer.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("FramePilot VR")
        title.setObjectName("Title")
        subtitle = QLabel("实时帧预算控制台 · 默认只读")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.language_combo = QComboBox()
        for language_code, language_label in LANGUAGE_OPTIONS:
            self.language_combo.addItem(language_label, language_code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(self.language))
        )
        self.language_combo.currentIndexChanged.connect(self.language_changed)
        header.addWidget(self.language_combo)
        self.guide_button = QPushButton("使用引导")
        self.guide_button.clicked.connect(lambda: self.show_onboarding(force=True))
        header.addWidget(self.guide_button)
        header.addSpacing(12)
        self.advanced_check = QCheckBox("高级模式")
        self.advanced_check.setChecked(self.advanced_mode)
        self.advanced_check.toggled.connect(self.set_advanced_mode)
        header.addWidget(self.advanced_check)
        header.addSpacing(12)
        self.app_label = QLabel("等待场景应用")
        self.app_label.setObjectName("Muted")
        self.connection_label = QLabel("正在连接")
        self.connection_label.setObjectName("StatusWait")
        header.addWidget(self.app_label)
        header.addSpacing(12)
        header.addWidget(self.connection_label)
        outer.addLayout(header)

        self.write_status_label = QLabel()
        self.write_status_label.setWordWrap(True)
        self.write_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.write_status_label)
        self._update_write_status_banner()

        cards = QGridLayout()
        cards.setHorizontalSpacing(11)
        self.gpu_card = MetricCard("GPU 帧时间 P95", "—", "帧预算 —")
        self.cpu_card = MetricCard("CPU 帧时间 P95", "—", "系统 CPU —")
        self.util_card = MetricCard("系统 GPU 占用", "—", "NVIDIA GPU")
        self.scale_card = MetricCard(self.tr("等效分辨率（单眼）"), "—", "SteamVR —")
        for i, card in enumerate((self.gpu_card, self.cpu_card, self.util_card, self.scale_card)):
            cards.addWidget(card, 0, i)
        outer.addLayout(cards)

        body = QHBoxLayout()
        body.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(12)

        chart_card = QFrame()
        chart_card.setObjectName("Card")
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(12, 12, 12, 10)
        chart_header = QHBoxLayout()
        chart_title = QLabel("最近 3 分钟性能趋势")
        chart_title.setStyleSheet("font-weight: 650; font-size: 14px;")
        self.hmd_label = QLabel("HMD —")
        self.hmd_label.setObjectName("Muted")
        self.hmd_label.setMaximumWidth(520)
        chart_header.addWidget(chart_title)
        chart_header.addStretch()
        chart_header.addWidget(self.hmd_label)
        chart_layout.addLayout(chart_header)
        self.chart = PerformanceChart()
        chart_layout.addWidget(self.chart)
        left.addWidget(chart_card, 1)
        body.addLayout(left, 7)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 2, 0)
        controls_layout.setSpacing(12)

        self.mode_group = QGroupBox("运行模式")
        mode_layout = QVBoxLayout(self.mode_group)
        self.preset_combo = QComboBox()
        for preset_name in PRESETS:
            self.preset_combo.addItem(preset_name, preset_name)
        self.preset_combo.setCurrentIndex(self.preset_combo.findData("平衡"))
        self.preset_combo.currentIndexChanged.connect(self.load_preset)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("只读监控", "monitor")
        self.mode_combo.addItem("连续自适应", "continuous")
        self.mode_combo.currentIndexChanged.connect(self.apply_config)
        mode_layout.addWidget(QLabel("控制预设"))
        mode_layout.addWidget(self.preset_combo)
        mode_layout.addWidget(QLabel("执行方式"))
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addWidget(QLabel("目标帧率预算"))
        self.target_fps_combo = QComboBox()
        self.target_fps_combo.addItem("原生刷新率", 1)
        self.target_fps_combo.addItem("刷新率的 1/2", 2)
        self.target_fps_combo.addItem("刷新率的 1/3", 3)
        self.target_fps_combo.addItem("刷新率的 1/4", 4)
        self.target_fps_combo.currentIndexChanged.connect(self.apply_config)
        mode_layout.addWidget(self.target_fps_combo)
        target_hint = QLabel("仅改变动态分辨率预算，不是游戏限帧器")
        target_hint.setWordWrap(True)
        target_hint.setObjectName("Muted")
        mode_layout.addWidget(target_hint)
        self.steamvr_autostart_check = QCheckBox("启动 FramePilot VR 时自动启动 SteamVR")
        self.steamvr_autostart_check.setChecked(
            setting_bool(self.settings, "startup/steamvr_autostart", False)
        )
        self.steamvr_autostart_check.setToolTip(
            self.tr("保存此选项；从下次启动 FramePilot VR 起生效。")
        )
        self.steamvr_autostart_check.toggled.connect(
            self.steamvr_autostart_changed
        )
        mode_layout.addWidget(self.steamvr_autostart_check)
        self.arm_check = QCheckBox("允许 FramePilot VR 调控 SteamVR 分辨率")
        self.arm_check.setToolTip(
            self.tr(
                "未勾选时只监控和推荐，不会写入 SteamVR。"
                "勾选后，单步、连续和手动操作可修改当前游戏的 resolutionScale；此选择会保存到本机。"
            )
        )
        self.arm_check.stateChanged.connect(self.arm_changed)
        self.restore_exit_check = QCheckBox("退出时恢复启动值")
        self.restore_exit_check.setChecked(True)
        mode_layout.addWidget(self.arm_check)
        controls_layout.addWidget(self.mode_group)

        self.collection_group = QGroupBox("匿名负载采集")
        collection_layout = QVBoxLayout(self.collection_group)
        self.collection_enabled_check = QCheckBox("正常游玩时自动采集（仅保存在本机）")
        collection_setting = self.settings.value("collection/enabled", True)
        collection_enabled = (
            collection_setting
            if isinstance(collection_setting, bool)
            else str(collection_setting).strip().lower() in {"1", "true", "yes", "on"}
        )
        self.collection_enabled_check.setChecked(collection_enabled)
        self.collection_enabled_check.toggled.connect(self.collection_enabled_changed)
        collection_layout.addWidget(self.collection_enabled_check)
        self.collection_auto_upload_check = QCheckBox("自动上传尚未上传的匿名记录")
        self.collection_auto_upload_check.setChecked(
            setting_bool(self.settings, "collection/auto_upload", False)
        )
        self.collection_auto_upload_check.toggled.connect(
            self.auto_upload_enabled_changed
        )
        collection_layout.addWidget(self.collection_auto_upload_check)
        collection_hint = QLabel(
            "自动过滤加载期并汇总稳定负载与人数变化；不会控制 VRChat。"
            "自动上传可在首次使用引导或此处随时取消。"
        )
        collection_hint.setWordWrap(True)
        collection_hint.setObjectName("Muted")
        collection_layout.addWidget(collection_hint)
        self.collection_status_label = QLabel("等待有效采集记录")
        self.collection_status_label.setWordWrap(True)
        self.collection_status_label.setObjectName("Muted")
        collection_layout.addWidget(self.collection_status_label)
        self.collection_export_button = QPushButton("导出共享数据")
        self.collection_export_button.clicked.connect(self.export_collection)
        self.collection_export_button.setEnabled(False)
        self.collection_upload_button = QPushButton("上传共享数据")
        self.collection_upload_button.clicked.connect(self.upload_collection)
        self.collection_upload_button.setEnabled(False)
        collection_buttons = QHBoxLayout()
        collection_buttons.addWidget(self.collection_export_button)
        collection_buttons.addWidget(self.collection_upload_button)
        collection_layout.addLayout(collection_buttons)
        controls_layout.addWidget(self.collection_group)

        self.overlay_group = QGroupBox("VR 参数叠加层")
        overlay_layout = QVBoxLayout(self.overlay_group)
        self.overlay_enabled_check = QCheckBox("在头显中显示参数")
        self.overlay_enabled_check.toggled.connect(self.overlay_enabled_changed)
        overlay_layout.addWidget(self.overlay_enabled_check)
        self.overlay_status_label = QLabel("未启用")
        self.overlay_status_label.setObjectName("Muted")
        overlay_layout.addWidget(self.overlay_status_label)
        overlay_hint = QLabel("透明 OSD · 无图表 · 勾选需要显示的参数")
        overlay_hint.setWordWrap(True)
        overlay_hint.setObjectName("Muted")
        overlay_layout.addWidget(overlay_hint)
        overlay_grid = QGridLayout()
        overlay_grid.setHorizontalSpacing(10)
        overlay_grid.setVerticalSpacing(5)
        self.overlay_field_checks: dict[str, QCheckBox] = {}
        for index, (field, label) in enumerate(OVERLAY_FIELD_OPTIONS):
            checkbox = QCheckBox(label)
            checkbox.setChecked(field in DEFAULT_OVERLAY_FIELDS)
            checkbox.toggled.connect(self.overlay_fields_changed)
            self.overlay_field_checks[field] = checkbox
            overlay_grid.addWidget(checkbox, index // 2, index % 2)
        overlay_layout.addLayout(overlay_grid)
        self.overlay_advanced = QWidget()
        overlay_advanced_layout = QGridLayout(self.overlay_advanced)
        overlay_advanced_layout.setContentsMargins(0, 4, 0, 0)
        self.overlay_anchor_combo = QComboBox()
        self.overlay_anchor_combo.addItem("左上", "upper_left")
        self.overlay_anchor_combo.addItem("右上", "upper_right")
        self.overlay_anchor_combo.addItem("左下", "lower_left")
        self.overlay_anchor_combo.addItem("右下", "lower_right")
        self.overlay_anchor_combo.currentIndexChanged.connect(self.overlay_config_changed)
        self.overlay_size_spin = make_spin(70, 140, 100, "%")
        self.overlay_size_spin.valueChanged.connect(self.overlay_config_changed)
        overlay_advanced_layout.addWidget(QLabel("头显位置"), 0, 0)
        overlay_advanced_layout.addWidget(self.overlay_anchor_combo, 0, 1)
        overlay_advanced_layout.addWidget(QLabel("显示大小"), 1, 0)
        overlay_advanced_layout.addWidget(self.overlay_size_spin, 1, 1)
        overlay_layout.addWidget(self.overlay_advanced)
        controls_layout.addWidget(self.overlay_group)

        self.range_group = QGroupBox("分辨率调节范围与规则")
        grid = QGridLayout(self.range_group)
        self.min_spin = make_spin(20, 500, 30, "%")
        self.max_spin = make_spin(20, 500, 150, "%")
        self.down_spin = make_spin(1, 20, 1, "%")
        self.up_spin = make_spin(1, 100, 5, "%")
        self.cooldown_spin = make_double_spin(0, 60, 8, " s")
        self.stable_spin = make_double_spin(0, 120, 12, " s")
        self.up_observation_spin = make_double_spin(0.5, 5.0, 2.0, " s")
        self.rollback_cooldown_spin = make_double_spin(0, 300, 20.0, " s")
        self.up_gpu_limit_spin = make_double_spin(50, 100, 92.0, "%")
        controls_list = [
            ("最低", self.min_spin),
            ("最高", self.max_spin),
            ("降档步长", self.down_spin),
            ("升档步长", self.up_spin),
            ("升档冷却", self.cooldown_spin),
            ("升档观察", self.stable_spin),
            ("升档后保护", self.up_observation_spin),
            ("回退后禁升", self.rollback_cooldown_spin),
            ("升档 GPU 上限", self.up_gpu_limit_spin),
        ]
        for row, (label, widget) in enumerate(controls_list):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
            widget.valueChanged.connect(lambda _value: self.apply_config())
        rules_hint = QLabel("程序只自动改变分辨率，不会自行修改这些规则。")
        rules_hint.setWordWrap(True)
        rules_hint.setObjectName("Muted")
        grid.addWidget(rules_hint, len(controls_list), 0, 1, 2)
        self.save_config_button = QPushButton("应用控制参数")
        self.save_config_button.clicked.connect(self.apply_config)
        grid.addWidget(self.save_config_button, len(controls_list) + 1, 0, 1, 2)
        self.save_config_button.setVisible(False)
        controls_layout.addWidget(self.range_group)

        self.manual_group = QGroupBox("手动验证")
        manual_layout = QVBoxLayout(self.manual_group)
        self.manual_spin = make_spin(20, 500, 100, "%")
        self.manual_apply = QPushButton("应用一次")
        self.manual_apply.setObjectName("Primary")
        self.manual_apply.clicked.connect(self.manual_apply_clicked)
        restore_button = QPushButton("恢复面板启动值")
        restore_button.clicked.connect(self.restore_clicked)
        manual_layout.addWidget(QLabel("目标分辨率"))
        manual_layout.addWidget(self.manual_spin)
        manual_layout.addWidget(self.manual_apply)
        manual_layout.addWidget(restore_button)
        self.decision_label = QLabel("等待性能数据")
        self.decision_label.setWordWrap(True)
        self.decision_label.setObjectName("Muted")
        manual_layout.addWidget(self.decision_label)
        controls_layout.addWidget(self.manual_group)

        self.experiment_group = QGroupBox("30 FPS A/B 调度实验")
        experiment_layout = QVBoxLayout(self.experiment_group)
        experiment_hint = QLabel("A：100% 起步预测升档 · B：150% 起步每次缓降 1% · 每轮 30 秒，各做 3 轮")
        experiment_hint.setWordWrap(True)
        experiment_hint.setObjectName("Muted")
        experiment_layout.addWidget(experiment_hint)
        experiment_buttons = QHBoxLayout()
        self.experiment_a_button = QPushButton("开始 A 组")
        self.experiment_b_button = QPushButton("开始 B 组")
        self.experiment_a_button.clicked.connect(lambda: self.start_experiment("A"))
        self.experiment_b_button.clicked.connect(lambda: self.start_experiment("B"))
        experiment_buttons.addWidget(self.experiment_a_button)
        experiment_buttons.addWidget(self.experiment_b_button)
        experiment_layout.addLayout(experiment_buttons)
        self.experiment_progress_bar = QProgressBar()
        self.experiment_progress_bar.setRange(0, 100)
        self.experiment_progress_bar.setValue(0)
        experiment_layout.addWidget(self.experiment_progress_bar)
        qualification_text = " · 高位缓降已通过本机门槛" if self.high_start_qualified else ""
        self.experiment_status_label = QLabel(f"A 0/3 · B 0/3 · 等待同场景测试{qualification_text}")
        self.experiment_status_label.setWordWrap(True)
        self.experiment_status_label.setObjectName("Muted")
        experiment_layout.addWidget(self.experiment_status_label)
        controls_layout.addWidget(self.experiment_group)
        self.experiment_group.setVisible(SHOW_AB_EXPERIMENT_UI)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        controls_scroll.setWidget(controls)
        controls_scroll.setMinimumWidth(330)
        controls_scroll.setMaximumWidth(420)
        controls_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        body.addWidget(controls_scroll, 3, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(body)

        log_card = QFrame()
        log_card.setObjectName("Card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 10, 12, 12)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("事件与写入记录"))
        log_header.addStretch()
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(lambda: self.event_log.clear())
        log_header.addWidget(clear_button)
        log_layout.addLayout(log_header)
        self.event_log = QTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMinimumHeight(105)
        log_layout.addWidget(self.event_log)
        outer.addWidget(log_card, 1)

        self.load_preset("平衡")

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = MonitorWorker(
            executable_dir() / "logs", self.collection_enabled_check.isChecked()
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.snapshot.connect(self.update_snapshot)
        self.worker.event.connect(self.append_event)
        self.worker.connection.connect(self.update_connection)
        self.worker.experiment_progress.connect(self.update_experiment_progress)
        self.worker.experiment_finished.connect(self.experiment_complete)
        self.worker.collection_status.connect(self.update_collection_status)
        self.worker.collection_exported.connect(self.collection_export_complete)
        self.worker.collection_uploaded.connect(self.collection_upload_complete)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()
        QTimer.singleShot(200, self.apply_config)

    def _restore_overlay_config(self) -> None:
        raw_fields = self.settings.value("overlay/fields", list(DEFAULT_OVERLAY_FIELDS))
        if isinstance(raw_fields, str):
            restored = [field for field in raw_fields.split(",") if field]
        else:
            restored = [str(field) for field in raw_fields]
        if str(self.settings.value("overlay/vrc_context_added", "false")).lower() != "true":
            if "vrc_context" not in restored:
                restored.append("vrc_context")
            self.settings.setValue("overlay/vrc_context_added", True)
            self.settings.setValue("overlay/fields", restored)
            self.settings.sync()
        allowed = {field for field, _label in OVERLAY_FIELD_OPTIONS}
        restored = [field for field in restored if field in allowed]
        for field, checkbox in self.overlay_field_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(field in restored)
            checkbox.blockSignals(False)
        enabled = str(self.settings.value("overlay/enabled", "false")).lower() == "true"
        self.overlay_enabled_check.blockSignals(True)
        self.overlay_enabled_check.setChecked(enabled)
        self.overlay_enabled_check.blockSignals(False)
        anchor = str(self.settings.value("overlay/anchor", "upper_left"))
        anchor_index = self.overlay_anchor_combo.findData(anchor)
        self.overlay_anchor_combo.blockSignals(True)
        self.overlay_anchor_combo.setCurrentIndex(max(0, anchor_index))
        self.overlay_anchor_combo.blockSignals(False)
        try:
            size_pct = int(self.settings.value("overlay/size_pct", 100))
        except (TypeError, ValueError):
            size_pct = 100
        self.overlay_size_spin.blockSignals(True)
        self.overlay_size_spin.setValue(max(70, min(140, size_pct)))
        self.overlay_size_spin.blockSignals(False)
        self._set_overlay_status("starting" if enabled else "disabled")

    def selected_overlay_fields(self) -> list[str]:
        return [
            field
            for field, _label in OVERLAY_FIELD_OPTIONS
            if self.overlay_field_checks[field].isChecked()
        ]

    def overlay_fields_changed(self, _checked: bool = False) -> None:
        fields = self.selected_overlay_fields()
        self.settings.setValue("overlay/fields", fields)
        self.settings.sync()
        self._send_overlay_settings()

    def overlay_config_changed(self, _value=None) -> None:
        self.settings.setValue("overlay/anchor", str(self.overlay_anchor_combo.currentData()))
        self.settings.setValue("overlay/size_pct", self.overlay_size_spin.value())
        self.settings.sync()
        self._send_overlay_settings()

    def _send_overlay_settings(self) -> None:
        if hasattr(self, "worker"):
            self.worker.submit_overlay_settings(
                self.selected_overlay_fields(),
                str(self.overlay_anchor_combo.currentData()),
                self.overlay_size_spin.value(),
                self.language,
            )

    def overlay_enabled_changed(self, enabled: bool) -> None:
        self.settings.setValue("overlay/enabled", bool(enabled))
        self.settings.sync()
        self._sync_overlay_process()
        self._send_overlay_settings()

    def steamvr_autostart_changed(self, enabled: bool) -> None:
        self.settings.setValue("startup/steamvr_autostart", bool(enabled))
        self.settings.sync()

    def _autostart_steamvr_if_enabled(self) -> None:
        autostart_suppressed = os.environ.get(
            "FRAMEPILOT_SUPPRESS_STEAMVR_AUTOSTART",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        if autostart_suppressed or not self.steamvr_autostart_check.isChecked():
            return
        state, detail = request_steamvr_start()
        if state == "already_running":
            self.append_event("info", self.tr("SteamVR 已在运行，已跳过自动启动"))
        elif state == "requested":
            self.append_event("success", self.tr("已请求 Steam 启动 SteamVR"))
        else:
            prefix = self.tr("无法自动启动 SteamVR")
            self.append_event("error", f"{prefix}: {detail}")

    def collection_enabled_changed(self, enabled: bool) -> None:
        self.settings.setValue("collection/enabled", bool(enabled))
        self.settings.sync()
        if hasattr(self, "worker"):
            self.worker.submit_collection_enabled(enabled)

    def set_auto_upload_enabled(self, enabled: bool) -> None:
        self.collection_auto_upload_check.blockSignals(True)
        self.collection_auto_upload_check.setChecked(bool(enabled))
        self.collection_auto_upload_check.blockSignals(False)
        self.auto_upload_enabled_changed(bool(enabled))

    def auto_upload_enabled_changed(self, enabled: bool) -> None:
        self.settings.setValue("collection/auto_upload", bool(enabled))
        self.settings.sync()
        if not enabled:
            self._auto_upload_retry_timer.stop()
            return
        QTimer.singleShot(0, self._maybe_auto_upload)

    def _persist_auto_upload_state(self) -> None:
        values = {
            "auto_upload_last_success_records": self._auto_upload_last_success_records,
            "auto_upload_last_attempt_at": self._auto_upload_last_attempt_at,
            "auto_upload_next_allowed_at": self._auto_upload_next_allowed_at,
            "upload_rate_limited_until": self._upload_rate_limited_until,
            "auto_upload_failure_count": self._auto_upload_failure_count,
        }
        for key, value in values.items():
            self.settings.setValue(f"collection/{key}", value)
        self.settings.sync()

    def _upload_cooldown_remaining(self) -> int:
        return max(
            0,
            math.ceil(self._upload_rate_limited_until - time.time()),
        )

    def _refresh_collection_upload_button(self) -> None:
        if self._collection_uploading:
            self.collection_upload_button.setText(self.tr("正在上传…"))
            self.collection_upload_button.setEnabled(False)
            return
        remaining = self._upload_cooldown_remaining()
        if remaining > 0:
            minutes = max(1, math.ceil(remaining / 60))
            self.collection_upload_button.setText(
                self.trf("{minutes} 分钟后重试", minutes=minutes)
            )
            self.collection_upload_button.setEnabled(False)
            return
        self.collection_upload_button.setText(self.tr("上传共享数据"))
        self.collection_upload_button.setEnabled(
            int(self.last_collection_status.get("records", 0)) > 0
        )

    def _schedule_auto_upload(self, wake_at: float) -> None:
        if wake_at <= 0.0 or not self.collection_auto_upload_check.isChecked():
            return
        delay_ms = max(1_000, math.ceil((wake_at - time.time()) * 1000))
        self._auto_upload_retry_timer.start(min(delay_ms, 2_147_000_000))
        self._refresh_collection_upload_button()

    def update_collection_status(self, data: dict) -> None:
        self.last_collection_status = dict(data)
        enabled = bool(data.get("enabled", False))
        worlds = int(data.get("worlds", 0))
        contexts = int(data.get("contexts", 0))
        steady = int(data.get("steady_records", 0))
        transitions = int(data.get("transition_records", 0))
        size_kib = float(data.get("storage_bytes", 0)) / 1024.0
        prefix = self.tr("采集中" if enabled else "已暂停")
        text = self.trf(
            "{prefix} · 世界 {worlds}/30 · 世界/人数场景 {contexts}\n"
            "稳定窗口 {steady} · 人数变化 {transitions} · {size_kib:.1f} KiB",
            prefix=prefix,
            worlds=worlds,
            contexts=contexts,
            steady=steady,
            transitions=transitions,
            size_kib=size_kib,
        )
        self.collection_status_label.setText(text)
        has_records = int(data.get("records", 0)) > 0
        self.collection_export_button.setEnabled(has_records)
        self._refresh_collection_upload_button()
        records_path = str(data.get("records_path", ""))
        self.collection_status_label.setToolTip(records_path)
        self._maybe_auto_upload()

    def _maybe_auto_upload(self, force: bool = False) -> None:
        if (
            not self.collection_auto_upload_check.isChecked()
            or self._collection_uploading
            or not hasattr(self, "worker")
        ):
            return
        records = int(self.last_collection_status.get("records", 0))
        if records <= 0:
            return
        if records < self._auto_upload_last_success_records:
            self._auto_upload_last_success_records = 0
            self._persist_auto_upload_state()
        now = time.time()
        next_allowed = max(
            self._auto_upload_next_allowed_at,
            self._upload_rate_limited_until,
        )
        due, wake_at = auto_upload_due(
            records,
            self._auto_upload_last_success_records,
            now,
            self._auto_upload_last_attempt_at,
            next_allowed,
            force=force,
        )
        if not due:
            self._schedule_auto_upload(wake_at)
            return
        self._begin_collection_upload(automatic=True)

    def export_collection(self) -> None:
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("导出匿名共享数据"),
            str(Path.home() / "framepilot-vr-shared-data.zip"),
            self.tr("ZIP 压缩包 (*.zip)"),
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        self.collection_export_button.setEnabled(False)
        self.worker.submit_collection_export(path)

    def collection_export_complete(self, data: dict) -> None:
        self.collection_export_button.setEnabled(
            int(self.last_collection_status.get("records", 0)) > 0
        )
        if not bool(data.get("ok", False)):
            QMessageBox.warning(
                self,
                self.tr("导出失败"),
                str(data.get("error", "Unknown error")),
            )
            return
        path = str(data.get("path", ""))
        self.append_event("success", f"{self.tr('匿名共享数据已导出')}: {path}")
        QMessageBox.information(
            self,
            self.tr("导出完成"),
            self.trf(
                "可供后期上传的压缩包已保存到：\n{path}\n\n"
                "其中不包含玩家身份、实例 ID、机器名或硬件指纹。",
                path=path,
            ),
        )

    def upload_collection(self) -> None:
        if self._collection_uploading:
            return
        remaining = self._upload_cooldown_remaining()
        if remaining > 0:
            minutes = max(1, math.ceil(remaining / 60))
            QMessageBox.information(
                self,
                self.tr("上传暂缓"),
                self.trf(
                    "服务器要求暂缓上传，请在约 {minutes} 分钟后重试。"
                    "本地记录仍安全保留，没有被删除。",
                    minutes=minutes,
                ),
            )
            return
        title = self.tr("上传共享数据")
        message = self.tr(
            "是否把尚未上传的本地记录发送到 FramePilot 共享服务？\n\n"
            "包含：世界 ID、人数区间与变化、GPU/HMD 型号、GPU 显存、"
            "CPU 型号与核心/线程数、系统内存、渲染设置和聚合性能指标。\n\n"
            "不包含：玩家身份、VRChat 实例 ID、机器名或硬件指纹。"
            "只有本次确认后才会上传。"
        )
        answer = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._begin_collection_upload(automatic=False)

    def _begin_collection_upload(self, automatic: bool) -> None:
        if self._collection_uploading:
            return
        self._collection_uploading = True
        self._collection_upload_is_automatic = automatic
        if automatic:
            now = time.time()
            self._auto_upload_last_attempt_at = now
            self._auto_upload_next_allowed_at = (
                now + AUTO_UPLOAD_MIN_INTERVAL_SECONDS
            )
            self._persist_auto_upload_state()
        self._refresh_collection_upload_button()
        self.append_event(
            "info",
            self.tr(
                "开始自动上传新增匿名记录"
                if automatic
                else "开始上传匿名共享数据"
            ),
        )
        self.worker.submit_collection_upload(automatic=automatic)

    def collection_upload_complete(self, data: dict) -> None:
        automatic = bool(
            data.get("automatic", self._collection_upload_is_automatic)
        )
        self._collection_uploading = False
        self._collection_upload_is_automatic = False
        if not bool(data.get("ok", False)):
            error = str(data.get("error", "Unknown error"))
            status = int(data.get("http_status", 0))
            retry_after = int(data.get("retry_after_seconds", 0))
            self._auto_upload_failure_count += 1
            fallback = min(
                AUTO_UPLOAD_MAX_BACKOFF_SECONDS,
                AUTO_UPLOAD_MIN_INTERVAL_SECONDS
                * (2 ** min(3, self._auto_upload_failure_count - 1)),
            )
            wait_seconds = retry_after if retry_after > 0 else fallback
            now = time.time()
            self._auto_upload_next_allowed_at = now + wait_seconds
            if status == 429:
                self._upload_rate_limited_until = now + wait_seconds
            self._persist_auto_upload_state()
            minutes = max(1, math.ceil(wait_seconds / 60))
            friendly = self.trf(
                "上传暂时受限，本地记录仍安全保留；约 {minutes} 分钟后重试。",
                minutes=minutes,
            )
            self.append_event("warning", friendly)
            self._schedule_auto_upload(self._auto_upload_next_allowed_at)
            if automatic:
                return
            QMessageBox.warning(
                self,
                self.tr("上传失败"),
                f"{friendly}\n\n{error}",
            )
            return
        batches = int(data.get("batches", 0))
        accepted = int(data.get("accepted_records", 0))
        duplicates = int(data.get("duplicate_records", 0))
        has_more = bool(data.get("has_more", False))
        self._auto_upload_retry_timer.stop()
        now = time.time()
        records = int(self.last_collection_status.get("records", 0))
        self._auto_upload_last_success_records = (
            max(0, records - 1) if has_more else records
        )
        self._auto_upload_last_attempt_at = now
        self._auto_upload_next_allowed_at = (
            now + AUTO_UPLOAD_MIN_INTERVAL_SECONDS
        )
        self._upload_rate_limited_until = 0.0
        self._auto_upload_failure_count = 0
        self._persist_auto_upload_state()
        self._refresh_collection_upload_button()
        if batches == 0:
            detail = self.tr("没有需要上传的新记录。")
        else:
            detail = self.trf(
                "上传完成：服务器接收 {accepted} 条，重复 {duplicates} 条，"
                "共 {batches} 个批次。",
                accepted=accepted,
                duplicates=duplicates,
                batches=batches,
            )
        if has_more:
            detail += self.tr("\n仍有记录未上传，请再次点击“上传共享数据”继续。")
        self.append_event("success", detail.replace("\n", " "))
        if automatic:
            if has_more and self.collection_auto_upload_check.isChecked():
                self._schedule_auto_upload(self._auto_upload_next_allowed_at)
            return
        QMessageBox.information(
            self,
            self.tr("上传完成"),
            detail,
        )

    def _set_overlay_status(self, state: str, detail: str = "") -> None:
        labels = {
            "disabled": "未启用",
            "starting": "正在启动",
            "waiting_steamvr": "等待 SteamVR",
            "waiting_scene": "等待场景",
            "active": "正常显示",
            "error": "错误",
        }
        self.overlay_state = state
        text = self.tr(labels.get(state, state))
        if detail and state == "error":
            text += f" · {detail}"
        self.overlay_status_label.setText(text)
        self.overlay_status_label.setToolTip(detail)
        self.overlay_status_label.setStyleSheet(
            "color:#6FE0B1" if state == "active" else "color:#FF7C91" if state == "error" else "color:#F2C36B" if state != "disabled" else ""
        )

    def _read_overlay_status(self) -> None:
        if self.overlay_process is None:
            return
        self._overlay_stdout_buffer += bytes(self.overlay_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        while "\n" in self._overlay_stdout_buffer:
            line, self._overlay_stdout_buffer = self._overlay_stdout_buffer.split("\n", 1)
            try:
                status = json.loads(line)
            except (TypeError, ValueError):
                continue
            self._apply_overlay_status(status)

    def _apply_overlay_status(self, status: object) -> None:
        if not isinstance(status, dict):
            return
        state = str(status.get("state", "error"))
        detail = str(status.get("detail", ""))
        self._set_overlay_status(state, detail)
        if state == "waiting_scene":
            QTimer.singleShot(100, self._send_overlay_settings)

    def _poll_overlay_status_file(self) -> None:
        try:
            contents = self._overlay_status_path.read_text(encoding="ascii")
        except OSError:
            return
        if contents == self._overlay_status_contents:
            return
        self._overlay_status_contents = contents
        try:
            status = json.loads(contents)
        except (TypeError, ValueError):
            return
        self._apply_overlay_status(status)

    def _overlay_started(self) -> None:
        self._set_overlay_status("starting")
        self._overlay_status_timer.start()
        QTimer.singleShot(300, self._send_overlay_settings)

    def _overlay_finished(self, exit_code: int, _status) -> None:
        self._overlay_status_timer.stop()
        expected_stop = self._overlay_expected_stop
        self._overlay_expected_stop = False
        if self._closing or expected_stop or not self.overlay_enabled_check.isChecked():
            self._set_overlay_status("disabled")
            return
        self._set_overlay_status("error", f"Overlay 进程已退出 ({exit_code})")
        self.append_event("error", f"VR 叠加层异常退出，代码 {exit_code}")

    def _overlay_process_error(self, _error) -> None:
        if self._closing or self._overlay_expected_stop or not self.overlay_enabled_check.isChecked():
            return
        if self.overlay_process is None:
            return
        message = self.overlay_process.errorString()
        self._set_overlay_status("error", message)
        self.append_event("error", f"VR 叠加层启动失败: {message}")

    def _ensure_overlay_process(self) -> QProcess:
        if self.overlay_process is None:
            process = QProcess(self)
            process.readyReadStandardOutput.connect(self._read_overlay_status)
            process.started.connect(self._overlay_started)
            process.finished.connect(self._overlay_finished)
            process.errorOccurred.connect(self._overlay_process_error)
            self.overlay_process = process
        return self.overlay_process

    def _sync_overlay_process(self) -> None:
        process = self._ensure_overlay_process()
        if self.overlay_enabled_check.isChecked():
            if process.state() == QProcess.ProcessState.NotRunning:
                self._overlay_expected_stop = False
                self._overlay_status_contents = ""
                self._overlay_status_path.unlink(missing_ok=True)
                if getattr(sys, "frozen", False):
                    process.setWorkingDirectory(str(Path(sys.executable).parent))
                    process.setProgram(sys.executable)
                    process.setArguments(
                        [
                            "--overlay-process",
                            "--overlay-status-file",
                            str(self._overlay_status_path),
                        ]
                    )
                else:
                    script = Path(__file__).with_name("steamvr_overlay.py")
                    process.setWorkingDirectory(str(script.parent))
                    process.setProgram(sys.executable)
                    process.setArguments([str(script)])
                process.start()
                self.append_event("info", "VR 参数叠加层已启动；SteamVR 未运行时会自动等待")
        elif process.state() != QProcess.ProcessState.NotRunning:
            self._overlay_expected_stop = True
            self._overlay_status_timer.stop()
            process.terminate()
            if not process.waitForFinished(1200):
                process.kill()
                process.waitForFinished(500)
        else:
            self._overlay_status_timer.stop()
            self._set_overlay_status("disabled")

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        self.show_action = QAction("显示面板", self)
        self.show_action.triggered.connect(self.showNormal)
        self.exit_action = QAction("退出", self)
        self.exit_action.triggered.connect(self.close)
        menu.addAction(self.show_action)
        menu.addSeparator()
        menu.addAction(self.exit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("FramePilot VR")
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def maybe_show_onboarding(self) -> None:
        completed = setting_bool(self.settings, "onboarding/completed", False)
        try:
            revision = int(self.settings.value("onboarding/revision", 0))
        except (TypeError, ValueError):
            revision = 0
        if not completed or revision < ONBOARDING_REVISION:
            self.show_onboarding(force=False)

    def show_onboarding(self, force: bool = False) -> None:
        if self._closing:
            return
        if self.onboarding_dialog is not None and self.onboarding_dialog.isVisible():
            self.onboarding_dialog.raise_()
            self.onboarding_dialog.activateWindow()
            return
        if not force:
            value = self.settings.value("onboarding/completed", False)
            completed = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
            try:
                revision = int(self.settings.value("onboarding/revision", 0))
            except (TypeError, ValueError):
                revision = 0
            if completed and revision >= ONBOARDING_REVISION:
                return
        self.onboarding_dialog = OnboardingDialog(self)
        self.onboarding_dialog.finished.connect(lambda _result: setattr(self, "onboarding_dialog", None))
        self.onboarding_dialog.open()

    def tr(self, chinese: str) -> str:
        return LOCALIZER.translate(chinese, self.language)

    def trf(self, chinese: str, **values: object) -> str:
        return LOCALIZER.format(chinese, self.language, **values)

    def localize_message(self, message: str) -> str:
        return LOCALIZER.localize_message(message, self.language)

    def language_changed(self) -> None:
        language = str(self.language_combo.currentData())
        if language not in SUPPORTED_LANGUAGES or language == self.language:
            return
        self.language = language
        self.settings.setValue("language", language)
        self.retranslate_ui(announce=True)
        self._send_overlay_settings()

    def set_advanced_mode(self, enabled: bool, persist: bool = True) -> None:
        self.advanced_mode = bool(enabled)
        if self.advanced_check.isChecked() != self.advanced_mode:
            self.advanced_check.blockSignals(True)
            self.advanced_check.setChecked(self.advanced_mode)
            self.advanced_check.blockSignals(False)
        one_step_index = self.mode_combo.findData("one_step")
        if self.advanced_mode and one_step_index < 0:
            self.mode_combo.insertItem(1, self.tr("单步自动调整"), "one_step")
        elif not self.advanced_mode and one_step_index >= 0:
            if self.mode_combo.currentData() == "one_step":
                self.mode_combo.setCurrentIndex(self.mode_combo.findData("monitor"))
            self.mode_combo.removeItem(one_step_index)
        self.range_group.setVisible(self.advanced_mode)
        self.manual_group.setVisible(self.advanced_mode)
        self.overlay_advanced.setVisible(self.advanced_mode)
        self.experiment_group.setVisible(
            SHOW_AB_EXPERIMENT_UI and self.advanced_mode
        )
        self.save_config_button.setVisible(False)
        if persist:
            self.settings.setValue("advanced_mode", self.advanced_mode)
        if hasattr(self, "worker"):
            self.apply_config()

    def _set_legacy_target(self, target_fps: float) -> None:
        self.legacy_target_fps = float(target_fps)
        index = self.target_fps_combo.findData(0)
        if index < 0:
            self.target_fps_combo.addItem("", 0)
            index = self.target_fps_combo.count() - 1
        self.target_fps_combo.setCurrentIndex(index)
        self._refresh_target_labels()

    def _refresh_target_labels(self, refresh_hz: float | None = None) -> None:
        if refresh_hz is None and self.last_snapshot:
            refresh_hz = float(self.last_snapshot.get("refresh_hz", 0.0))
        names = {
            1: "原生刷新率",
            2: "刷新率的 1/2",
            3: "刷新率的 1/3",
            4: "刷新率的 1/4",
        }
        for index in range(self.target_fps_combo.count()):
            divisor = int(self.target_fps_combo.itemData(index))
            if divisor == 0:
                prefix = self.tr("旧策略自定义")
                text = f"{prefix} · {self.legacy_target_fps:g} FPS"
            else:
                text = self.tr(names[divisor])
                if refresh_hz and refresh_hz > 1:
                    text += f" · {refresh_hz / divisor:g} FPS"
            self.target_fps_combo.setItemText(index, text)

    def retranslate_ui(self, announce: bool = False) -> None:
        def translated(current: str) -> str:
            return LOCALIZER.translate(current, self.language)

        for widget_type in (QLabel, QPushButton, QCheckBox):
            for widget in self.findChildren(widget_type):
                widget.setText(translated(widget.text()))
        for group in self.findChildren(QGroupBox):
            group.setTitle(translated(group.title()))
        for index in range(self.preset_combo.count()):
            data = str(self.preset_combo.itemData(index))
            self.preset_combo.setItemText(index, self.tr(data) if data in PRESETS or data == "自定义/已迁移" else data)
        mode_names = {"monitor": "只读监控", "one_step": "单步自动调整", "continuous": "连续自适应"}
        for index in range(self.mode_combo.count()):
            self.mode_combo.setItemText(index, self.tr(mode_names[str(self.mode_combo.itemData(index))]))
        anchor_names = {
            "upper_left": "左上",
            "upper_right": "右上",
            "lower_left": "左下",
            "lower_right": "右下",
        }
        for index in range(self.overlay_anchor_combo.count()):
            anchor = str(self.overlay_anchor_combo.itemData(index))
            self.overlay_anchor_combo.setItemText(index, self.tr(anchor_names[anchor]))
        self.steamvr_autostart_check.setToolTip(
            self.tr("保存此选项；从下次启动 FramePilot VR 起生效。")
        )
        self.arm_check.setToolTip(
            self.tr(
                "未勾选时只监控和推荐，不会写入 SteamVR。"
                "勾选后，单步、连续和手动操作可修改当前游戏的 resolutionScale；此选择会保存到本机。"
            )
        )
        self._refresh_target_labels()
        if hasattr(self, "show_action"):
            self.show_action.setText(self.tr("显示面板"))
            self.exit_action.setText(self.tr("退出"))
        if self.onboarding_dialog is not None:
            self.onboarding_dialog.retranslate_ui()
        self.chart.language = self.language
        self.chart.update()
        if self.last_snapshot:
            self.update_snapshot(self.last_snapshot)
        if self.last_collection_status:
            self.update_collection_status(self.last_collection_status)
        self._update_write_status_banner()
        self.update_connection(*self.connection_state)
        if announce:
            message = f"{self.tr('语言已切换')} · {self.language_combo.currentText()}"
            self.append_event("info", message)

    def _restore_cached_config(self) -> None:
        self.arm_check.blockSignals(True)
        self.arm_check.setChecked(cached_write_permission(self.settings))
        self.arm_check.blockSignals(False)
        if not self.settings.contains("runtime/mode"):
            self.settings.sync()
            return

        def number(key: str, default: float, converter):
            try:
                return converter(float(self.settings.value(key, default)))
            except (TypeError, ValueError):
                return converter(default)

        self._loading_controls = True
        try:
            preset = str(self.settings.value("runtime/preset", "平衡"))
            preset_index = self.preset_combo.findData(preset)
            if preset_index < 0 and preset == "自定义/已迁移":
                self.preset_combo.addItem(self.tr("自定义/已迁移"), preset)
                preset_index = self.preset_combo.count() - 1
            if preset_index >= 0:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(preset_index)
                self.preset_combo.blockSignals(False)

            scheduler_revision = number("runtime/scheduler_revision", 0, int)
            self.min_spin.setValue(number("runtime/min_scale", 30, int))
            self.max_spin.setValue(number("runtime/max_scale", 150, int))
            self.down_spin.setValue(1 if scheduler_revision < 1 else number("runtime/step_down", 1, int))
            self.up_spin.setValue(number("runtime/step_up", 5, int))
            self.cooldown_spin.setValue(number("runtime/cooldown_seconds", 8.0, float))
            self.stable_spin.setValue(number("runtime/raise_stable_seconds", 12.0, float))
            self.policy_window_seconds = number("runtime/window_seconds", 3.0, float)
            self.policy_evaluate_seconds = 0.25
            self.policy_gpu_down_ratio = number("runtime/gpu_down_ratio", 0.92, float)
            self.policy_gpu_raise_ratio = number("runtime/gpu_raise_ratio", 0.72, float)
            self.policy_cpu_raise_ratio = number("runtime/cpu_raise_ratio", 0.80, float)
            self.up_observation_spin.setValue(number("runtime/up_observation_seconds", 2.0, float))
            self.rollback_cooldown_spin.setValue(number("runtime/up_rollback_cooldown_seconds", 20.0, float))
            self.up_gpu_limit_spin.setValue(number("runtime/up_gpu_limit_pct", 92.0, float))

            divisor = number("runtime/target_divisor", 1, int)
            legacy_fps = number("runtime/target_fps", 0.0, float)
            if divisor == 0 and legacy_fps > 0:
                self._set_legacy_target(legacy_fps)
            elif divisor in {1, 2, 3, 4}:
                target_index = self.target_fps_combo.findData(divisor)
                if target_index >= 0:
                    self.target_fps_combo.setCurrentIndex(target_index)

            mode = str(self.settings.value("runtime/mode", "monitor"))
            if mode == "one_step" and self.mode_combo.findData(mode) < 0:
                self.set_advanced_mode(True, persist=False)
            mode_index = self.mode_combo.findData(mode)
            if mode_index >= 0:
                self.mode_combo.setCurrentIndex(mode_index)

        finally:
            self._loading_controls = False
        self.settings.sync()
        self._update_write_status_banner()

    def _cache_config(self, config: RuntimeConfig) -> None:
        values = {
            "scheduler_revision": 1,
            "preset": str(self.preset_combo.currentData()),
            "mode": config.mode,
            "target_divisor": config.target_divisor,
            "target_fps": config.target_fps,
            "min_scale": config.min_scale,
            "max_scale": config.max_scale,
            "step_down": config.step_down,
            "step_up": config.step_up,
            "window_seconds": config.window_seconds,
            "evaluate_seconds": config.evaluate_seconds,
            "cooldown_seconds": config.cooldown_seconds,
            "raise_stable_seconds": config.raise_stable_seconds,
            "gpu_down_ratio": config.gpu_down_ratio,
            "gpu_raise_ratio": config.gpu_raise_ratio,
            "cpu_raise_ratio": config.cpu_raise_ratio,
            "up_observation_seconds": config.up_observation_seconds,
            "up_rollback_cooldown_seconds": config.up_rollback_cooldown_seconds,
            "up_gpu_limit_pct": config.up_gpu_limit_pct,
            "armed": config.armed,
        }
        for key, value in values.items():
            self.settings.setValue(f"runtime/{key}", value)
        self.settings.sync()

    def config_from_ui(self) -> RuntimeConfig:
        target_divisor = int(self.target_fps_combo.currentData())
        return RuntimeConfig(
            mode=str(self.mode_combo.currentData()),
            armed=self.arm_check.isChecked(),
            target_divisor=target_divisor,
            target_fps=self.legacy_target_fps if target_divisor == 0 else 0.0,
            min_scale=self.min_spin.value(),
            max_scale=self.max_spin.value(),
            step_down=self.down_spin.value(),
            step_up=self.up_spin.value(),
            window_seconds=self.policy_window_seconds,
            evaluate_seconds=self.policy_evaluate_seconds,
            cooldown_seconds=self.cooldown_spin.value(),
            raise_stable_seconds=self.stable_spin.value(),
            gpu_down_ratio=self.policy_gpu_down_ratio,
            gpu_raise_ratio=self.policy_gpu_raise_ratio,
            cpu_raise_ratio=self.policy_cpu_raise_ratio,
            up_observation_seconds=self.up_observation_spin.value(),
            up_rollback_cooldown_seconds=self.rollback_cooldown_spin.value(),
            up_gpu_limit_pct=self.up_gpu_limit_spin.value(),
            startup_scale=150 if str(self.preset_combo.currentData()) == "激进" and self.high_start_qualified else 0,
            restore_on_exit=True,
        ).validated()

    def load_preset(self, _selection=None) -> None:
        name = str(self.preset_combo.currentData())
        if name not in PRESETS:
            return
        values = PRESETS[name]
        self._loading_controls = True
        self.min_spin.setValue(int(values["min_scale"]))
        self.max_spin.setValue(int(values["max_scale"]))
        self.down_spin.setValue(int(values["step_down"]))
        self.up_spin.setValue(int(values["step_up"]))
        self.cooldown_spin.setValue(float(values["cooldown_seconds"]))
        self.stable_spin.setValue(float(values["raise_stable_seconds"]))
        self.policy_gpu_down_ratio = float(values["gpu_down_ratio"])
        self.policy_gpu_raise_ratio = float(values["gpu_raise_ratio"])
        self.policy_cpu_raise_ratio = 0.80
        self.policy_window_seconds = 3.0
        self.policy_evaluate_seconds = 0.25
        self._loading_controls = False
        if hasattr(self, "worker"):
            self.apply_config()

    def apply_config(self) -> None:
        if self._loading_controls or not hasattr(self, "worker"):
            return
        try:
            config = self.config_from_ui()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        self.worker.submit_config(config)
        self.manual_apply.setEnabled(config.armed)
        self._update_write_status_banner(config)
        if self._skip_next_cache:
            self._skip_next_cache = False
        else:
            self._cache_config(config)

    def _select_custom_policy(self) -> None:
        index = self.preset_combo.findData("自定义/已迁移")
        if index < 0:
            self.preset_combo.addItem(self.tr("自定义/已迁移"), "自定义/已迁移")
            index = self.preset_combo.count() - 1
        self.preset_combo.setCurrentIndex(index)

    def export_policy(self) -> None:
        try:
            config = self.config_from_ui()
        except ValueError as exc:
            QMessageBox.warning(self, self.tr("参数错误"), str(exc))
            return
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("导出便携策略"),
            str(Path.home() / "steamvr-adaptive-policy.json"),
            self.tr("JSON 策略 (*.json)"),
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            StrategyStore.export_portable(path, config, path.stem)
            self.append_event("success", f"便携策略已导出: {path}")
            QMessageBox.information(
                self,
                self.tr("导出完成"),
                self.tr("已导出阈值、步长和时间窗口。硬件指纹与本机最终分辨率未被导出。"),
            )
        except Exception as exc:
            QMessageBox.warning(self, self.tr("导出失败"), str(exc))

    def import_policy(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("导入便携策略"),
            str(Path.home()),
            self.tr("JSON 策略 (*.json)"),
        )
        if not path_text:
            return
        try:
            imported = StrategyStore.import_portable(Path(path_text), self.config_from_ui())
            self._loading_controls = True
            if imported.target_divisor == 0:
                self._set_legacy_target(imported.target_fps)
            else:
                target_index = self.target_fps_combo.findData(int(imported.target_divisor))
                self.target_fps_combo.setCurrentIndex(target_index)
            self.min_spin.setValue(imported.min_scale)
            self.max_spin.setValue(imported.max_scale)
            self.down_spin.setValue(imported.step_down)
            self.up_spin.setValue(imported.step_up)
            self.cooldown_spin.setValue(imported.cooldown_seconds)
            self.stable_spin.setValue(imported.raise_stable_seconds)
            self.policy_gpu_down_ratio = imported.gpu_down_ratio
            self.policy_gpu_raise_ratio = imported.gpu_raise_ratio
            self.policy_cpu_raise_ratio = imported.cpu_raise_ratio
            self.policy_window_seconds = imported.window_seconds
            self.policy_evaluate_seconds = imported.evaluate_seconds
            self._select_custom_policy()
            self._loading_controls = False
            self.arm_check.blockSignals(True)
            self.arm_check.setChecked(False)
            self.arm_check.blockSignals(False)
            self.mode_combo.setCurrentIndex(0)
            self.apply_config()
            self.calibration_status.setText(
                self.tr("策略已迁移；当前保持只读，请在本机运行校准后采用分辨率范围")
            )
            self.append_event("success", f"已导入便携策略: {path_text}")
        except Exception as exc:
            self._loading_controls = False
            QMessageBox.warning(self, self.tr("导入失败"), str(exc))

    def start_calibration(self, precise: bool) -> None:
        if not self.last_snapshot:
            QMessageBox.information(
                self,
                self.tr("等待数据"),
                self.tr("需要先进入 VR 游戏并取得稳定帧时序。"),
            )
            return
        if precise:
            if not self.arm_check.isChecked():
                QMessageBox.information(
                    self,
                    self.tr("写入锁定"),
                    self.tr("精确阶梯校准需要先允许 FramePilot VR 调控 SteamVR 分辨率。"),
                )
                return
            answer = QMessageBox.warning(
                self,
                self.tr("开始精确阶梯校准"),
                self.tr("校准会短暂测试当前值、-10% 和 +10%，结束后自动恢复原值。请保持在同一代表性场景。"),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return
        self.apply_config()
        self.worker.submit_calibration(precise, float(self.calibration_duration.value()))
        self.readonly_calibrate.setEnabled(False)
        self.precise_calibrate.setEnabled(False)
        self.calibration_status.setText(
            self.tr("正在校准；请保持游戏场景与视角尽量稳定")
        )

    def update_hardware(self, data: dict) -> None:
        self.hardware_context = data
        self._profile_key = ""

    def update_calibration_progress(self, data: dict) -> None:
        self.calibration_progress.setValue(int(data["percent"]))
        self.calibration_status.setText(self.localize_message(str(data["text"])))
        if bool(data.get("done", False)):
            self.readonly_calibrate.setEnabled(True)
            self.precise_calibrate.setEnabled(True)

    def calibration_complete(self, data: dict) -> None:
        if "error" in data:
            self.calibration_progress.setValue(0)
            prefix = self.tr("校准失败")
            self.calibration_status.setText(f"{prefix}: {self.localize_message(str(data['error']))}")
            self.readonly_calibrate.setEnabled(True)
            self.precise_calibrate.setEnabled(True)
            return
        self.local_store.load()
        self.calibration_progress.setValue(100)
        recommended = int(data["recommended_scale"])
        self.min_spin.setValue(int(data["recommended_min"]))
        self.max_spin.setValue(int(data["recommended_max"]))
        self.manual_spin.setValue(recommended)
        self._select_custom_policy()
        self.apply_config()
        bound = (
            f" ({self.tr('CPU 受限，建议未主动降分辨率')})"
            if bool(data["cpu_bound"])
            else ""
        )
        precision = self.tr("精确阶梯" if bool(data["precise"]) else "只读估算")
        self.calibration_status.setText(
            self.trf(
                "场景校准完成 · {precision} · 建议 {recommended}% · "
                "范围 {minimum}–{maximum}%{bound}",
                precision=precision,
                recommended=recommended,
                minimum=data["recommended_min"],
                maximum=data["recommended_max"],
                bound=bound,
            )
        )
        self.readonly_calibrate.setEnabled(True)
        self.precise_calibrate.setEnabled(True)

    def arm_changed(self, state: int) -> None:
        if state and not self._closing:
            result = QMessageBox.warning(
                self,
                self.tr("允许修改分辨率"),
                self.tr(
                    "允许后，单步、连续和手动模式都可以修改当前游戏的 SteamVR resolutionScale。\n\n"
                    "此选择会保存在本机，直到你主动取消。建议先使用只读模式观察，再进行单步验证。"
                ),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if result != QMessageBox.StandardButton.Ok:
                self.arm_check.blockSignals(True)
                self.arm_check.setChecked(False)
                self.arm_check.blockSignals(False)
        self.apply_config()
        self._update_write_status_banner()

    def _update_write_status_banner(
        self,
        config: RuntimeConfig | None = None,
    ) -> None:
        if not hasattr(self, "write_status_label"):
            return
        if config is None:
            try:
                config = self.config_from_ui()
            except (AttributeError, TypeError, ValueError):
                config = RuntimeConfig()
        if not config.armed:
            text = self.tr("只读监控 · 不会修改 SteamVR")
            colors = ("#0F2B25", "#6FE0B1", "#245E50")
        elif config.mode == "continuous":
            text = self.tr("连续控制已启用 · 分辨率可能持续变化")
            colors = ("#3A171C", "#FF9AAA", "#7A303B")
        else:
            text = self.tr("已允许写入 · 单步和手动操作可能修改分辨率")
            colors = ("#382B12", "#F2C36B", "#725923")
        background, foreground, border = colors
        self.write_status_label.setText(text)
        self.write_status_label.setStyleSheet(
            f"background:{background};color:{foreground};border:1px solid {border};"
            "border-radius:7px;padding:8px;font-weight:650;"
        )

    def start_experiment(self, variant: str) -> None:
        if not self.last_snapshot:
            QMessageBox.information(self, "等待 VRChat", "请先进入稳定的 VRChat 场景并取得帧时序。")
            return
        if not self.arm_check.isChecked():
            QMessageBox.information(
                self,
                "写入锁定",
                "A/B 测试会主动修改分辨率，请先允许 FramePilot VR 调控 SteamVR 分辨率。",
            )
            return
        start_scale = 100 if variant == "A" else 150
        answer = QMessageBox.warning(
            self,
            f"开始 {variant} 组测试",
            f"本轮固定使用 30 FPS 预算并从 {start_scale}% 开始，持续 30 秒，结束后恢复当前配置和分辨率。\n\n请保持世界、视角和动作不变。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.apply_config()
        self.experiment_a_button.setEnabled(False)
        self.experiment_b_button.setEnabled(False)
        self.experiment_progress_bar.setValue(0)
        self.experiment_status_label.setText(f"{variant} 组准备中…")
        self.worker.submit_experiment(variant)

    def update_experiment_progress(self, data: dict) -> None:
        self.experiment_progress_bar.setValue(int(data.get("percent", 0)))
        if not bool(data.get("done", False)):
            self.experiment_status_label.setText(
                f"{data.get('variant', '')} 组第 {data.get('run', 1)} 轮 · {float(data.get('elapsed', 0.0)):.0f}/30 秒"
            )

    def experiment_complete(self, data: dict) -> None:
        self.experiment_a_button.setEnabled(True)
        self.experiment_b_button.setEnabled(True)
        if "error" in data:
            self.experiment_status_label.setText(f"测试未完成: {data['error']}")
            return
        counts = data.get("counts", {"A": 0, "B": 0})
        a_count = int(counts.get("A", 0)) if isinstance(counts, dict) else 0
        b_count = int(counts.get("B", 0)) if isinstance(counts, dict) else 0
        base = (
            f"A {a_count}/3 · B {b_count}/3 · 本轮调档峰值 {float(data.get('adjustment_peak_ms', 0.0)):.2f} ms"
        )
        if "qualified" in data:
            qualified = bool(data["qualified"])
            self.high_start_qualified = qualified
            self.settings.setValue("experiment/high_start_qualified", qualified)
            self.settings.sync()
            verdict = (
                f"高位缓降已通过：尖峰降低 {float(data.get('peak_reduction_pct', 0.0)):.1f}%，已加入激进预设"
                if qualified
                else "高位缓降未达到门槛；继续使用预测升档和 2 秒回退保护"
            )
            base += f"\n{verdict}"
            self.apply_config()
        self.experiment_status_label.setText(base)

    def manual_apply_clicked(self) -> None:
        if not self.arm_check.isChecked():
            QMessageBox.information(
                self,
                self.tr("写入锁定"),
                self.tr("请先勾选“允许 FramePilot VR 调控 SteamVR 分辨率”。"),
            )
            return
        current = self.last_snapshot.get("resolution_scale", "—")
        target = self.manual_spin.value()
        answer = QMessageBox.question(
            self,
            self.tr("应用分辨率"),
            self.trf(
                "将当前场景应用从 {current}% 调整为 {target}%？",
                current=current,
                target=target,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.worker.submit_manual_scale(target)

    def restore_clicked(self) -> None:
        self.worker.submit_restore()

    def update_connection(self, connected: bool, text: str) -> None:
        self.connection_state = (connected, text)
        self.connection_label.setText(self.localize_message(text))
        self.connection_label.setObjectName("StatusGood" if connected else "StatusWait")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

    def update_snapshot(self, data: dict) -> None:
        self.last_snapshot = data
        gpu = float(data["gpu_p95_ms"])
        cpu = float(data["cpu_p95_ms"])
        budget = float(data["budget_ms"])
        refresh = float(data["refresh_hz"])
        target_fps = float(data["target_fps"])
        scale = int(data["resolution_scale"])
        sys_gpu = data["system_gpu_pct"]
        sys_cpu = float(data["system_cpu_pct"])
        proposed = int(data["proposed_scale"])
        app_key = str(data["app_key"])
        render_width = int(data["render_width"])
        render_height = int(data["render_height"])
        dimension_ratio = math.sqrt(max(scale, 1) / 100.0)
        equivalent_width = round(render_width * dimension_ratio)
        equivalent_height = round(render_height * dimension_ratio)

        self.app_label.setText(app_key)
        self._refresh_target_labels(refresh)
        target_divisor = int(data.get("target_divisor", self.target_fps_combo.currentData()))
        cadence = (
            self.tr("原生")
            if target_divisor == 1
            else f"1/{target_divisor}"
            if target_divisor > 1
            else self.tr("旧策略")
        )
        target_text = self.trf("目标 {cadence} ({fps:g} FPS)", cadence=cadence, fps=target_fps)
        self.gpu_card.set_values(
            f"{gpu:.2f} ms",
            self.trf(
                "帧预算 {budget:.2f} ms · {ratio:.0f}%",
                budget=budget,
                ratio=gpu / budget * 100,
            ),
        )
        self.cpu_card.set_values(
            f"{cpu:.2f} ms",
            self.trf("系统 CPU {value:.0f}%", value=sys_cpu),
        )
        self.util_card.set_values("n/a" if sys_gpu is None else f"{float(sys_gpu):.0f}%", "NVIDIA GPU")
        arrow = "=" if proposed == scale else "→"
        self.scale_card.set_values(
            f"{equivalent_width}×{equivalent_height}",
            self.trf(
                "SteamVR {scale}% · 建议 {arrow} {proposed}%",
                scale=scale,
                arrow=arrow,
                proposed=proposed,
            ),
        )
        hmd_text = self.trf(
            "{refresh:.0f} Hz · {target} · 基准 {width}×{height} · 写入 {count} 次",
            refresh=refresh,
            target=target_text,
            width=render_width,
            height=render_height,
            count=int(data["write_count"]),
        )
        self.hmd_label.setText(hmd_text)
        self.hmd_label.setToolTip(hmd_text)
        self.decision_label.setText(
            f"{self.localize_message(str(data['reason']))}\n"
            f"{self.trf('当前建议：{proposed}%', proposed=proposed)}"
        )
        if not self.manual_spin.hasFocus():
            self.manual_spin.setValue(scale)
        self.chart.add_point(gpu, cpu, budget)
        if bool(data["write_applied"]):
            self.append_event("write", f"自动应用 {scale}% · {data['reason']}")

    def append_event(self, level: str, message: str) -> None:
        colors = {
            "success": "#6FE0B1",
            "warning": "#F2C36B",
            "error": "#FF7C91",
            "write": "#77AFFF",
            "info": "#AFC0D5",
        }
        color = colors.get(level, "#AFC0D5")
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        message = self.localize_message(message)
        self.event_log.append(
            f'<span style="color:#60748A">[{stamp}]</span> '
            f'<span style="color:{color};font-weight:600">{html.escape(level.upper())}</span> '
            f'<span style="color:#DCE5EF">{html.escape(message)}</span>'
        )

    def capture_screenshot(self) -> None:
        if self.screenshot_path is None:
            return
        self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.grab().save(str(self.screenshot_path))
        self.append_event("success", f"截图已保存: {self.screenshot_path}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self.tray.hide()
        if self.overlay_process is not None and self.overlay_process.state() != QProcess.ProcessState.NotRunning:
            self._overlay_expected_stop = True
            self.overlay_process.terminate()
            if not self.overlay_process.waitForFinished(1200):
                self.overlay_process.kill()
                self.overlay_process.waitForFinished(500)
        self.worker.stop()
        self.thread.quit()
        if not self.thread.wait(6000):
            self.thread.quit()
            self.thread.wait(1000)
        event.accept()


def make_spin(minimum: int, maximum: int, value: int, suffix: str) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setSuffix(suffix)
    return widget


def make_double_spin(minimum: float, maximum: float, value: float, suffix: str) -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setDecimals(1)
    widget.setSingleStep(1.0)
    widget.setSuffix(suffix)
    return widget


def make_icon() -> QIcon:
    icon_path = resource_path("assets", "framepilot-vr-icon.png")
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#1677FF"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(4, 4, 56, 56), 15, 15)
    painter.setPen(QPen(QColor("#FFFFFF"), 4))
    painter.drawLine(QPointF(16, 38), QPointF(26, 27))
    painter.drawLine(QPointF(26, 27), QPointF(36, 34))
    painter.drawLine(QPointF(36, 34), QPointF(49, 18))
    painter.end()
    return QIcon(pixmap)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FramePilot VR desktop panel")
    parser.add_argument("--overlay-process", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--overlay-status-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--auto-close", type=float, default=0.0)
    parser.add_argument(
        "--language",
        choices=tuple(code for code, _label in LANGUAGE_OPTIONS),
        help="临时覆盖界面语言",
    )
    parser.add_argument("--target-divisor", type=int, choices=(1, 2, 3, 4), help="临时设置头显刷新率分频")
    parser.add_argument("--target-fps", type=float, help="高级兼容项：临时设置绝对 FPS 预算")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.overlay_process:
        from steamvr_overlay import run_overlay

        app = QApplication(sys.argv[:1])
        return run_overlay(args.overlay_status_file)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FramePilot VR")
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(True)
    app.setStyleSheet(STYLE)
    window = MainWindow(
        screenshot_path=args.screenshot,
        auto_close=args.auto_close,
        language_override=args.language,
        target_divisor_override=args.target_divisor,
        target_fps_override=args.target_fps,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
