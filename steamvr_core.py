from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import deque
from dataclasses import dataclass, replace

import psutil
import openvr

from vrc_context import (
    PassiveVrcDataCollector,
    VrcContextSnapshot,
    VrcLogContextProvider,
    VrcResolutionProfileStore,
    target_key as vrc_target_key,
)


APP_NAME = "FramePilot VR"
RESOLUTION_KEY = "resolutionScale"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STRATEGY_SCHEMA_VERSION = 1
DOWN_WRITE_INTERVAL_SECONDS = 0.25
WRITE_RECOVERY_SECONDS = 5.0
TARGET_CHANGE_PROBE_RATIO = 0.85
TARGET_CHANGE_PROBE_MAX_SCALE = 150
TARGET_CHANGE_CADENCE_SECONDS = 0.75
TARGET_CHANGE_TIMEOUT_SECONDS = 20.0
GPU_SOFT_CAP_STEP = 10
GPU_SATURATED_SOFT_CAP_STEP = 5
PORTABLE_CONFIG_FIELDS = (
    "target_divisor",
    "target_fps",
    "min_scale",
    "max_scale",
    "step_down",
    "step_up",
    "window_seconds",
    "evaluate_seconds",
    "cooldown_seconds",
    "raise_stable_seconds",
    "gpu_down_ratio",
    "gpu_raise_ratio",
    "cpu_raise_ratio",
)


def ensure_utf8_console() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


@dataclass(frozen=True)
class FrameSample:
    index: int
    timestamp: float
    gpu_ms: float
    cpu_ms: float
    interval_ms: float
    dropped: int
    mispresented: int
    reprojection: bool


@dataclass(frozen=True)
class WindowStats:
    frames: int
    gpu_p50_ms: float
    gpu_p95_ms: float
    cpu_p95_ms: float
    interval_p95_ms: float
    reprojection_pct: float
    dropped: int
    mispresented: int


@dataclass(frozen=True)
class Decision:
    action: str
    proposed_scale: int
    reason: str


@dataclass
class WriteObservation:
    write_id: int
    app_key: str
    action: str
    reason: str
    from_scale: int
    to_scale: int
    started_at: float
    pre_gpu_ms: float
    pre_frame_ms: float
    pre_gpu_pct: float | None
    peak_gpu_ms: float = 0.0
    peak_frame_ms: float = 0.0
    peak_gpu_pct: float | None = None
    peak_reprojection_pct: float = 0.0
    dropped: int = 0
    mispresented: int = 0
    complete: bool = False
    rolled_back: bool = False

    def update(self, stats: WindowStats, system_gpu_pct: float | None, now: float) -> None:
        if self.complete:
            return
        self.peak_gpu_ms = max(self.peak_gpu_ms, stats.gpu_p95_ms)
        self.peak_frame_ms = max(self.peak_frame_ms, stats.interval_p95_ms)
        if system_gpu_pct is not None:
            self.peak_gpu_pct = max(self.peak_gpu_pct or 0.0, system_gpu_pct)
        self.peak_reprojection_pct = max(self.peak_reprojection_pct, stats.reprojection_pct)
        self.dropped = max(self.dropped, stats.dropped)
        self.mispresented = max(self.mispresented, stats.mispresented)
        if now - self.started_at >= WRITE_RECOVERY_SECONDS:
            self.complete = True


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str = "monitor"  # monitor | one_step | continuous
    armed: bool = False
    target_divisor: int = 1  # 1=native, 2=half, 3=third, 4=quarter, 0=custom/legacy
    target_fps: float = 0.0  # custom/legacy absolute target when target_divisor=0
    min_scale: int = 40
    max_scale: int = 150
    step_down: int = 1
    step_up: int = 5
    window_seconds: float = 3.0
    evaluate_seconds: float = 0.25
    cooldown_seconds: float = 8.0
    raise_stable_seconds: float = 12.0
    gpu_down_ratio: float = 0.92
    gpu_raise_ratio: float = 0.72
    cpu_raise_ratio: float = 0.80
    up_observation_seconds: float = 2.0
    up_rollback_cooldown_seconds: float = 20.0
    up_gpu_limit_pct: float = 92.0
    startup_scale: int = 0
    restore_on_exit: bool = True

    def validated(self) -> "RuntimeConfig":
        if self.mode not in {"monitor", "one_step", "continuous"}:
            raise ValueError("mode 必须是 monitor、one_step 或 continuous")
        if not 20 <= self.min_scale <= 500:
            raise ValueError("min_scale 必须在 20..500")
        if not self.min_scale <= self.max_scale <= 500:
            raise ValueError("max_scale 必须大于等于 min_scale，且不超过 500")
        if self.step_down <= 0 or self.step_up <= 0:
            raise ValueError("调节步长必须大于 0")
        if self.target_fps != 0 and not 20 <= self.target_fps <= 240:
            raise ValueError("target_fps 必须为 0（自动）或 20..240")
        if self.target_divisor not in {0, 1, 2, 3, 4}:
            raise ValueError("target_divisor 必须为 0..4")
        if self.target_divisor == 0 and self.target_fps <= 0:
            raise ValueError("自定义帧率模式需要 target_fps")
        if self.window_seconds <= 0 or self.evaluate_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("时间参数无效")
        if not 0.5 <= self.up_observation_seconds <= WRITE_RECOVERY_SECONDS:
            raise ValueError("升档观察必须在 0.5..5 秒")
        if not 0 <= self.up_rollback_cooldown_seconds <= 300:
            raise ValueError("回退冷却必须在 0..300 秒")
        if not 50 <= self.up_gpu_limit_pct <= 100:
            raise ValueError("升档 GPU 占用上限必须在 50..100%")
        if self.startup_scale != 0 and not 20 <= self.startup_scale <= 500:
            raise ValueError("startup_scale 必须为 0 或 20..500")
        return self


@dataclass(frozen=True)
class HardwareContext:
    hardware_id: str
    machine_name: str
    gpu_name: str
    hmd_manufacturer: str
    hmd_model: str
    refresh_hz: float
    render_width: int
    render_height: int
    cpu_name: str = "Unknown CPU"
    cpu_physical_cores: int = 0
    cpu_logical_cores: int = 0
    system_ram_mib: int = 0
    gpu_vram_mib: int = 0
    gpu_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "hardware_id": self.hardware_id,
            "machine_name": self.machine_name,
            "gpu_name": self.gpu_name,
            "hmd_manufacturer": self.hmd_manufacturer,
            "hmd_model": self.hmd_model,
            "refresh_hz": self.refresh_hz,
            "render_width": self.render_width,
            "render_height": self.render_height,
            "cpu_name": self.cpu_name,
            "cpu_physical_cores": self.cpu_physical_cores,
            "cpu_logical_cores": self.cpu_logical_cores,
            "system_ram_mib": self.system_ram_mib,
            "gpu_vram_mib": self.gpu_vram_mib,
            "gpu_count": self.gpu_count,
        }


@dataclass(frozen=True)
class CalibrationResult:
    app_key: str
    hardware_id: str
    precise: bool
    original_scale: int
    recommended_scale: int
    recommended_min: int
    recommended_max: int
    gpu_budget_ms: float
    samples: dict[str, dict[str, float]]
    cpu_bound: bool
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "app_key": self.app_key,
            "hardware_id": self.hardware_id,
            "precise": self.precise,
            "original_scale": self.original_scale,
            "recommended_scale": self.recommended_scale,
            "recommended_min": self.recommended_min,
            "recommended_max": self.recommended_max,
            "gpu_budget_ms": self.gpu_budget_ms,
            "samples": self.samples,
            "cpu_bound": self.cpu_bound,
            "created_at": self.created_at,
        }


def portable_policy(config: RuntimeConfig, name: str = "自定义") -> dict[str, object]:
    return {
        "schema_version": STRATEGY_SCHEMA_VERSION,
        "kind": "steamvr-adaptive-portable-policy",
        "name": name,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "policy": {field: getattr(config, field) for field in PORTABLE_CONFIG_FIELDS},
        "notes": "Portable thresholds only; machine-specific resolution calibration is intentionally excluded.",
    }


def config_from_portable_policy(data: dict[str, object], base: RuntimeConfig | None = None) -> RuntimeConfig:
    if data.get("kind") != "steamvr-adaptive-portable-policy":
        raise ValueError("不是 SteamVR Adaptive Portable Policy 文件")
    if int(data.get("schema_version", 0)) != STRATEGY_SCHEMA_VERSION:
        raise ValueError("策略文件版本不受支持")
    policy = data.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("策略文件缺少 policy")
    current = base or RuntimeConfig()
    values: dict[str, object] = {}
    for field in PORTABLE_CONFIG_FIELDS:
        default = getattr(current, field)
        raw = policy.get(field, default)
        values[field] = int(raw) if isinstance(default, int) else float(raw)
    if "target_divisor" not in policy and float(values["target_fps"]) > 0:
        values["target_divisor"] = 0
    return replace(current, **values, mode="monitor", armed=False).validated()


class StrategyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, object] = {
            "schema_version": STRATEGY_SCHEMA_VERSION,
            "local_profiles": {},
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and int(loaded.get("schema_version", 0)) == STRATEGY_SCHEMA_VERSION:
                self.data = loaded
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def save_calibration(self, context: HardwareContext, result: CalibrationResult) -> None:
        profiles = self.data.setdefault("local_profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            self.data["local_profiles"] = profiles
        hardware = profiles.setdefault(context.hardware_id, {"context": context.as_dict(), "apps": {}})
        if not isinstance(hardware, dict):
            hardware = {"context": context.as_dict(), "apps": {}}
            profiles[context.hardware_id] = hardware
        hardware["context"] = context.as_dict()
        apps = hardware.setdefault("apps", {})
        if not isinstance(apps, dict):
            apps = {}
            hardware["apps"] = apps
        apps[result.app_key] = result.as_dict()
        self.save()

    def calibration_for(self, context: HardwareContext, app_key: str) -> dict[str, object] | None:
        profiles = self.data.get("local_profiles", {})
        if not isinstance(profiles, dict):
            return None
        hardware = profiles.get(context.hardware_id)
        if not isinstance(hardware, dict):
            return None
        apps = hardware.get("apps", {})
        if not isinstance(apps, dict):
            return None
        result = apps.get(app_key)
        return result if isinstance(result, dict) else None

    @staticmethod
    def export_portable(path: Path, config: RuntimeConfig, name: str) -> None:
        path.write_text(json.dumps(portable_policy(config, name), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def import_portable(path: Path, base: RuntimeConfig | None = None) -> RuntimeConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("策略文件格式无效")
        return config_from_portable_policy(data, base)


@dataclass(frozen=True)
class TelemetrySnapshot:
    local_time: dt.datetime
    app_key: str
    app_pid: int
    refresh_hz: float
    target_divisor: int
    target_fps: float
    budget_ms: float
    render_width: int
    render_height: int
    resolution_scale: int
    gpu_p50_ms: float
    gpu_p95_ms: float
    cpu_p95_ms: float
    frame_interval_p95_ms: float
    reprojection_pct: float
    dropped: int
    mispresented: int
    system_cpu_pct: float
    system_gpu_pct: float | None
    vrc_world_id: str
    vrc_world_short: str
    vrc_population: int
    vrc_population_bucket: str
    vrc_context_ready: bool
    vrc_recent_joins: int
    vrc_recent_leaves: int
    vrc_population_delta_10s: int
    vrc_population_delta_60s: int
    vrc_seconds_since_population_change: float
    vrc_profile_safe_scale: int
    vrc_profile_unsafe_scale: int
    vrc_profile_samples: int
    vrc_profile_confidence: float
    decision: Decision
    write_applied: bool
    write_count: int
    sample_seq: int
    observation_phase: str
    write_id: int
    write_from_scale: int
    write_to_scale: int
    write_action: str
    write_reason: str
    write_pre_gpu_ms: float
    write_pre_frame_ms: float
    recovery_elapsed_s: float
    recovery_complete: bool
    recovery_rolled_back: bool
    recovery_peak_gpu_ms: float
    recovery_peak_frame_ms: float
    recovery_peak_gpu_pct: float | None
    recovery_peak_reprojection_pct: float
    recovery_dropped: int
    recovery_mispresented: int

    def as_dict(self) -> dict[str, object]:
        return {
            "local_time": self.local_time.isoformat(timespec="milliseconds"),
            "app_key": self.app_key,
            "app_pid": self.app_pid,
            "refresh_hz": self.refresh_hz,
            "target_divisor": self.target_divisor,
            "target_fps": self.target_fps,
            "budget_ms": self.budget_ms,
            "render_width": self.render_width,
            "render_height": self.render_height,
            "resolution_scale": self.resolution_scale,
            "gpu_p50_ms": self.gpu_p50_ms,
            "gpu_p95_ms": self.gpu_p95_ms,
            "cpu_p95_ms": self.cpu_p95_ms,
            "frame_interval_p95_ms": self.frame_interval_p95_ms,
            "reprojection_pct": self.reprojection_pct,
            "dropped": self.dropped,
            "mispresented": self.mispresented,
            "system_cpu_pct": self.system_cpu_pct,
            "system_gpu_pct": self.system_gpu_pct,
            "vrc_world_id": self.vrc_world_id,
            "vrc_world_short": self.vrc_world_short,
            "vrc_population": self.vrc_population,
            "vrc_population_bucket": self.vrc_population_bucket,
            "vrc_context_ready": self.vrc_context_ready,
            "vrc_recent_joins": self.vrc_recent_joins,
            "vrc_recent_leaves": self.vrc_recent_leaves,
            "vrc_population_delta_10s": self.vrc_population_delta_10s,
            "vrc_population_delta_60s": self.vrc_population_delta_60s,
            "vrc_seconds_since_population_change": self.vrc_seconds_since_population_change,
            "vrc_profile_safe_scale": self.vrc_profile_safe_scale,
            "vrc_profile_unsafe_scale": self.vrc_profile_unsafe_scale,
            "vrc_profile_samples": self.vrc_profile_samples,
            "vrc_profile_confidence": self.vrc_profile_confidence,
            "decision": self.decision.action,
            "proposed_scale": self.decision.proposed_scale,
            "reason": self.decision.reason,
            "write_applied": self.write_applied,
            "write_count": self.write_count,
            "sample_seq": self.sample_seq,
            "observation_phase": self.observation_phase,
            "write_id": self.write_id,
            "write_from_scale": self.write_from_scale,
            "write_to_scale": self.write_to_scale,
            "write_action": self.write_action,
            "write_reason": self.write_reason,
            "write_pre_gpu_ms": self.write_pre_gpu_ms,
            "write_pre_frame_ms": self.write_pre_frame_ms,
            "recovery_elapsed_s": self.recovery_elapsed_s,
            "recovery_complete": self.recovery_complete,
            "recovery_rolled_back": self.recovery_rolled_back,
            "recovery_peak_gpu_ms": self.recovery_peak_gpu_ms,
            "recovery_peak_frame_ms": self.recovery_peak_frame_ms,
            "recovery_peak_gpu_pct": self.recovery_peak_gpu_pct,
            "recovery_peak_reprojection_pct": self.recovery_peak_reprojection_pct,
            "recovery_dropped": self.recovery_dropped,
            "recovery_mispresented": self.recovery_mispresented,
        }


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def effective_frame_budget(
    refresh_hz: float,
    configured_target_fps: float = 0.0,
    target_divisor: int = 1,
) -> tuple[float, float]:
    if refresh_hz <= 1.0:
        raise ValueError("refresh_hz 必须大于 1")
    if target_divisor in {1, 2, 3, 4}:
        target_fps = refresh_hz / target_divisor
    else:
        target_fps = refresh_hz if configured_target_fps <= 0 else min(refresh_hz, configured_target_fps)
    return target_fps, 1000.0 / target_fps


def action_cooldown_seconds(action: str, config: RuntimeConfig) -> float:
    if action == "rollback":
        return 0.0
    return DOWN_WRITE_INTERVAL_SECONDS if action == "down" else config.cooldown_seconds


def summarize(samples: list[FrameSample]) -> WindowStats:
    return WindowStats(
        frames=len(samples),
        gpu_p50_ms=percentile([x.gpu_ms for x in samples], 0.50),
        gpu_p95_ms=percentile([x.gpu_ms for x in samples], 0.95),
        cpu_p95_ms=percentile([x.cpu_ms for x in samples], 0.95),
        interval_p95_ms=percentile([x.interval_ms for x in samples], 0.95),
        reprojection_pct=(100.0 * sum(x.reprojection for x in samples) / len(samples)) if samples else 0.0,
        dropped=sum(x.dropped for x in samples),
        mispresented=sum(x.mispresented for x in samples),
    )


class AdaptiveController:
    def __init__(self) -> None:
        self.stable_since: float | None = None

    def reset(self) -> None:
        self.stable_since = None

    def decide(
        self,
        stats: WindowStats,
        budget_ms: float,
        scale: int,
        now: float,
        config: RuntimeConfig,
        system_gpu_pct: float | None = None,
    ) -> Decision:
        if scale > config.max_scale:
            target = max(config.max_scale, scale - config.step_down)
            return Decision("down", target, "当前设置高于配置上限，逐步回落")

        gpu_over = stats.gpu_p95_ms >= budget_ms * config.gpu_down_ratio
        cpu_over = stats.cpu_p95_ms >= budget_ms * 0.92
        delivery_bad = stats.dropped > 0 or stats.mispresented > 0 or stats.reprojection_pct >= 3.0

        if gpu_over or (delivery_bad and stats.gpu_p95_ms >= budget_ms * 0.80):
            self.stable_since = None
            target = max(config.min_scale, scale - config.step_down) if scale >= config.min_scale else scale
            if target >= scale:
                return Decision("hold", scale, "GPU/交付压力高，但已到分辨率下限")
            return Decision("down", target, "GPU 帧时间或重投影超过安全阈值")

        if cpu_over and stats.gpu_p95_ms < budget_ms * 0.80:
            self.stable_since = None
            return Decision("hold", scale, "CPU 受限；降低分辨率通常无效")

        stable = (
            stats.gpu_p95_ms <= budget_ms * config.gpu_raise_ratio
            and stats.cpu_p95_ms <= budget_ms * config.cpu_raise_ratio
            and stats.reprojection_pct == 0.0
            and stats.dropped == 0
            and stats.mispresented == 0
        )
        if stable:
            if self.stable_since is None:
                self.stable_since = now
            stable_for = now - self.stable_since
            if stable_for >= config.raise_stable_seconds:
                self.stable_since = now
                target_gpu_ms = budget_ms * config.gpu_raise_ratio
                predicted = math.floor(scale * target_gpu_ms / max(stats.gpu_p95_ms, 0.1))
                target = min(config.max_scale, max(config.min_scale, predicted))
                gpu_soft_limited = system_gpu_pct is not None and system_gpu_pct >= config.up_gpu_limit_pct
                if gpu_soft_limited:
                    max_step = (
                        GPU_SATURATED_SOFT_CAP_STEP
                        if system_gpu_pct >= 98.0
                        else GPU_SOFT_CAP_STEP
                    )
                    target = min(target, scale + max_step)
                if scale >= config.max_scale:
                    return Decision("hold", scale, "性能余量充足，但已到分辨率上限")
                if target <= scale:
                    return Decision("hold", scale, "预测余量不足以升档")
                reason = f"性能余量已稳定 {stable_for:.0f} 秒；按 GPU 帧时间预测一步升档"
                if gpu_soft_limited:
                    reason += f"；系统 GPU {system_gpu_pct:.0f}% 时限制升幅"
                return Decision(
                    "up",
                    target,
                    reason,
                )
            return Decision("hold", scale, f"性能余量观察中 {stable_for:.0f}/{config.raise_stable_seconds:.0f} 秒")

        self.stable_since = None
        return Decision("hold", scale, "处于滞回区间")


class GpuUtilizationSampler:
    def __init__(self) -> None:
        self.nvidia_smi = self._find_nvidia_smi()
        self.last_value: float | None = None
        self.last_sample_at = 0.0
        self._gpu_name: str | None = None
        self._gpu_vram_mib: int | None = None
        self._gpu_count: int | None = None

    @staticmethod
    def _find_nvidia_smi() -> str | None:
        candidates = [
            shutil.which("nvidia-smi"),
            str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"),
            str(Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
        ]
        return next((x for x in candidates if x and Path(x).is_file()), None)

    def sample(self, now: float) -> float | None:
        if now - self.last_sample_at < 2.0:
            return self.last_value
        self.last_sample_at = now
        if not self.nvidia_smi:
            return None
        try:
            completed = subprocess.run(
                [self.nvidia_smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            values = [float(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
            self.last_value = max(values) if values else None
        except (OSError, ValueError, subprocess.SubprocessError):
            self.last_value = None
        return self.last_value

    def gpu_name(self) -> str:
        if self._gpu_name is not None:
            return self._gpu_name
        if not self.nvidia_smi:
            controllers = self._windows_video_controllers()
            names = [
                str(item.get("Name", "")).strip()
                for item in controllers
                if str(item.get("Name", "")).strip()
            ]
            self._gpu_name = " + ".join(names) if names else "Unknown GPU"
            self._gpu_count = len(names)
            return self._gpu_name
        try:
            completed = subprocess.run(
                [self.nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            self._gpu_name = " + ".join(names) if names else "Unknown GPU"
            self._gpu_count = len(names)
        except (OSError, subprocess.SubprocessError):
            self._gpu_name = "Unknown GPU"
        return self._gpu_name

    @staticmethod
    def _windows_video_controllers() -> list[dict[str, object]]:
        if os.name != "nt":
            return []
        powershell = shutil.which("powershell.exe")
        if not powershell:
            return []
        try:
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "Get-CimInstance Win32_VideoController | "
                        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=4.0,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            parsed = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        if isinstance(parsed, dict):
            return [parsed]
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    def gpu_vram_mib(self) -> int:
        if self._gpu_vram_mib is not None:
            return self._gpu_vram_mib
        values: list[int] = []
        if self.nvidia_smi:
            try:
                completed = subprocess.run(
                    [
                        self.nvidia_smi,
                        "--query-gpu=memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=True,
                    creationflags=CREATE_NO_WINDOW,
                )
                values = [
                    int(float(line.strip()))
                    for line in completed.stdout.splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError, subprocess.SubprocessError):
                values = []
        if not values:
            controllers = self._windows_video_controllers()
            for item in controllers:
                try:
                    memory_bytes = int(item.get("AdapterRAM", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if memory_bytes > 0:
                    values.append(round(memory_bytes / (1024 * 1024)))
            if self._gpu_count is None:
                self._gpu_count = len(controllers)
        self._gpu_vram_mib = max(values, default=0)
        return self._gpu_vram_mib

    def gpu_count(self) -> int:
        if self._gpu_count is None:
            self.gpu_name()
        return max(0, int(self._gpu_count or 0))


def system_cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _value_type = winreg.QueryValueEx(key, "ProcessorNameString")
            name = " ".join(str(value).split())
            if name:
                return name
        except (OSError, ImportError):
            pass
    return (
        " ".join((platform.processor() or "").split())
        or " ".join(os.environ.get("PROCESSOR_IDENTIFIER", "").split())
        or "Unknown CPU"
    )


def system_hardware_details() -> dict[str, object]:
    return {
        "cpu_name": system_cpu_name(),
        "cpu_physical_cores": int(psutil.cpu_count(logical=False) or 0),
        "cpu_logical_cores": int(psutil.cpu_count(logical=True) or 0),
        "system_ram_mib": round(psutil.virtual_memory().total / (1024 * 1024)),
    }


class SteamVRSession:
    def __init__(self) -> None:
        self.system = None
        self.compositor = None
        self.applications = None
        self.settings = None

    def connect(self) -> None:
        openvr.init(openvr.VRApplication_Background)
        self.system = openvr.VRSystem()
        self.compositor = openvr.VRCompositor()
        self.applications = openvr.VRApplications()
        self.settings = openvr.VRSettings()

    def close(self) -> None:
        try:
            openvr.shutdown()
        except Exception:
            pass

    def scene_application(self) -> tuple[int, str]:
        assert self.applications is not None
        pid = int(self.applications.getCurrentSceneProcessId())
        if pid == 0:
            return 0, ""
        try:
            return pid, self.applications.getApplicationKeyByProcessId(pid)
        except Exception:
            return pid, ""

    def refresh_rate(self) -> float:
        assert self.system is not None
        try:
            value = float(
                self.system.getFloatTrackedDeviceProperty(
                    openvr.k_unTrackedDeviceIndex_Hmd,
                    openvr.Prop_DisplayFrequency_Float,
                )
            )
            return value if value > 1.0 else 90.0
        except Exception:
            return 90.0

    def recommended_size(self) -> tuple[int, int]:
        assert self.system is not None
        try:
            return self.system.getRecommendedRenderTargetSize()
        except Exception:
            return 0, 0

    def hmd_string(self, property_id: int, fallback: str) -> str:
        assert self.system is not None
        try:
            value = self.system.getStringTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd,
                property_id,
            )
            return str(value).strip() or fallback
        except Exception:
            return fallback

    def get_scale(self, app_key: str) -> tuple[int, bool]:
        assert self.settings is not None
        try:
            return int(self.settings.getInt32(app_key, RESOLUTION_KEY)), True
        except Exception:
            return 100, False

    def set_scale(self, app_key: str, scale: int) -> None:
        assert self.settings is not None
        self.settings.setInt32(app_key, RESOLUTION_KEY, int(scale))

    def frame_batch(self, count: int = 128) -> list[FrameSample]:
        assert self.compositor is not None
        timing_array = (openvr.Compositor_FrameTiming * count)()
        timing_array[0].m_nSize = ctypes.sizeof(openvr.Compositor_FrameTiming)
        filled, timings = self.compositor.getFrameTimings(timing_array)
        output: list[FrameSample] = []
        for item in list(timings)[: int(filled)]:
            cpu_ms = max(
                0.0,
                float(item.m_flNewFrameReadyMs - item.m_flNewPosesReadyMs + item.m_flCompositorRenderCpuMs),
            )
            output.append(
                FrameSample(
                    index=int(item.m_nFrameIndex),
                    timestamp=float(item.m_flSystemTimeInSeconds),
                    gpu_ms=max(0.0, float(item.m_flTotalRenderGpuMs)),
                    cpu_ms=cpu_ms,
                    interval_ms=max(0.0, float(item.m_flClientFrameIntervalMs)),
                    dropped=int(item.m_nNumDroppedFrames),
                    mispresented=int(item.m_nNumMisPresented),
                    reprojection=bool(int(item.m_nReprojectionFlags) & int(openvr.VRCompositor_ReprojectionMotion)),
                )
            )
        return output


def process_running(name: str) -> bool:
    target = name.casefold()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").casefold() == target:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class AdaptiveRuntime:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = (config or RuntimeConfig()).validated()
        self.session = SteamVRSession()
        self.controller = AdaptiveController()
        self.gpu_sampler = GpuUtilizationSampler()
        self.history: deque[FrameSample] = deque(maxlen=1024)
        self.connected = False
        self.current_app: str | None = None
        self.current_pid = 0
        self.current_scale = 100
        self.last_frame = -1
        self.last_eval = 0.0
        self.last_write = -1e9
        self.write_count = 0
        self.sample_seq = 0
        self.write_observations: deque[WriteObservation] = deque(maxlen=32)
        self.up_blocked_until = -1e9
        self.pending_target_raise: dict[str, float | None] | None = None
        self.one_step_written = False
        self.original_scales: dict[str, int] = {}
        self.events: deque[tuple[str, str]] = deque(maxlen=256)
        self._waiting_reported = False
        self.hardware_context: HardwareContext | None = None
        local_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SteamVRAdaptiveResolution"
        self.vrc_context_provider = VrcLogContextProvider()
        self.vrc_profile_store = VrcResolutionProfileStore(local_root / "vrc-context-profiles.json")
        self.vrc_context = VrcContextSnapshot()
        self.vrc_context_key = ""
        self.vrc_context_stable_since = 0.0
        self.vrc_profile_applied_key = ""
        psutil.cpu_percent(interval=None)

    def emit(self, level: str, message: str) -> None:
        self.events.append((level, message))

    def drain_events(self) -> list[tuple[str, str]]:
        output = list(self.events)
        self.events.clear()
        return output

    def update_config(self, config: RuntimeConfig) -> None:
        validated = config.validated()
        if validated.mode != self.config.mode:
            self.one_step_written = False
        refresh_hz = self.hardware_context.refresh_hz if self.hardware_context is not None else 0.0
        if refresh_hz > 1.0:
            old_target_fps, _old_budget = effective_frame_budget(
                refresh_hz,
                self.config.target_fps,
                self.config.target_divisor,
            )
            new_target_fps, new_budget = effective_frame_budget(
                refresh_hz,
                validated.target_fps,
                validated.target_divisor,
            )
            if (
                new_target_fps < old_target_fps - 0.1
                and validated.armed
                and validated.mode == "continuous"
            ):
                self.pending_target_raise = {
                    "requested_at": time.monotonic(),
                    "matched_since": None,
                    "target_fps": new_target_fps,
                    "budget_ms": new_budget,
                }
                self.emit(
                    "info",
                    f"目标帧率降低 {old_target_fps:g} → {new_target_fps:g} FPS；等待实际节拍稳定后预测升档",
                )
            elif new_target_fps > old_target_fps + 0.1:
                self.pending_target_raise = None
        self.config = replace(validated)
        self.controller.reset()

    def connect(self) -> None:
        if self.connected:
            return
        if not process_running("vrserver.exe"):
            raise RuntimeError("SteamVR 未运行")
        self.session.connect()
        self.connected = True
        width, height = self.session.recommended_size()
        refresh = self.session.refresh_rate()
        manufacturer = self.session.hmd_string(openvr.Prop_ManufacturerName_String, "Unknown")
        model = self.session.hmd_string(openvr.Prop_ModelNumber_String, "Unknown HMD")
        identity = {
            "machine_name": platform.node() or "Unknown PC",
            "gpu_name": self.gpu_sampler.gpu_name(),
            "hmd_manufacturer": manufacturer,
            "hmd_model": model,
            "refresh_hz": round(refresh, 2),
            "render_width": int(width),
            "render_height": int(height),
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        details = system_hardware_details()
        details.update(
            {
                "gpu_vram_mib": self.gpu_sampler.gpu_vram_mib(),
                "gpu_count": self.gpu_sampler.gpu_count(),
            }
        )
        self.hardware_context = HardwareContext(
            hardware_id=digest,
            **identity,
            **details,
        )
        self.emit("success", f"已连接 SteamVR · {refresh:.1f} Hz · 推荐目标 {width}×{height}")
        self.emit("info", f"硬件配置 {model} · {self.gpu_sampler.gpu_name()} · ID {digest}")

    def disconnect(self) -> None:
        if self.connected:
            self.session.close()
        self.connected = False

    def hardware(self) -> HardwareContext:
        if not self.connected:
            self.connect()
        if self.hardware_context is None:
            raise RuntimeError("无法读取 SteamVR 硬件信息")
        return self.hardware_context

    def close(self, restore: bool | None = None) -> None:
        should_restore = self.config.restore_on_exit if restore is None else restore
        if should_restore and self.connected and self.write_count:
            self.restore_all()
        self.vrc_profile_store.save_if_due(time.monotonic(), force=True)
        self.disconnect()

    def _change_app(self, pid: int, app_key: str) -> None:
        self.current_pid = pid
        self.current_app = app_key
        self.history.clear()
        self.last_frame = -1
        self.controller.reset()
        self.one_step_written = False
        if app_key:
            self.current_scale, explicit = self.session.get_scale(app_key)
            self.original_scales.setdefault(app_key, self.current_scale)
            marker = "显式设置" if explicit else "默认值"
            self.emit("info", f"场景应用 {app_key} · PID {pid} · {self.current_scale}%（{marker}）")
            if self.config.armed and self.config.mode == "continuous" and self.config.startup_scale:
                target = max(self.config.min_scale, min(self.config.max_scale, self.config.startup_scale))
                self._write_scale(target, "激进预设高位起步，随后缓慢下探", "startup")
        else:
            self.emit("warning", "当前没有 VR 场景应用，等待游戏提交画面")

    def _update_vrc_context(self, app_key: str, now: float) -> None:
        if app_key.casefold() != "steam.app.438100":
            self.vrc_context = VrcContextSnapshot()
            self.vrc_context_key = ""
            self.vrc_context_stable_since = 0.0
            self.vrc_profile_applied_key = ""
            return
        previous = self.vrc_context
        current = self.vrc_context_provider.poll(now)
        self.vrc_context = current
        basic_key = (
            f"{current.world_id}|{current.population_bucket}"
            if current.world_id and current.population_bucket
            else ""
        )
        if basic_key != self.vrc_context_key:
            self.vrc_context_key = basic_key
            self.vrc_context_stable_since = now
            self.vrc_profile_applied_key = ""
            if current.world_id:
                state = (
                    f"{current.population} 人 · 人数档 {current.population_bucket}"
                    if current.population_bucket
                    else "人数统计中"
                )
                self.emit("info", f"VRC 上下文 {current.world_short} · {state}")
        elif current.last_population_change_at > previous.last_population_change_at:
            self.vrc_context_stable_since = now

    def _vrc_profile_raise_decision(
        self,
        profile: dict[str, object] | None,
        profile_key: str,
        stats: WindowStats,
        budget_ms: float,
        system_gpu_pct: float | None,
        now: float,
    ) -> Decision | None:
        if (
            profile is None
            or not self.vrc_context.ready
            or not self.config.armed
            or self.config.mode != "continuous"
            or profile_key == self.vrc_profile_applied_key
            or now - self.vrc_context_stable_since < 3.0
            or now < self.up_blocked_until
        ):
            return None
        safe_scale = int(profile.get("safe_scale", 0))
        safe_evidence = int(profile.get("safe_evidence", 0))
        if safe_scale <= self.current_scale or safe_evidence < 8:
            return None
        live_stable = (
            stats.gpu_p95_ms <= budget_ms * self.config.gpu_raise_ratio
            and stats.cpu_p95_ms <= budget_ms * self.config.cpu_raise_ratio
            and stats.reprojection_pct == 0.0
            and stats.dropped == 0
            and stats.mispresented == 0
        )
        if not live_stable:
            return None
        predicted = math.floor(
            self.current_scale
            * budget_ms
            * TARGET_CHANGE_PROBE_RATIO
            / max(stats.gpu_p95_ms, 0.1)
        )
        target = min(self.config.max_scale, safe_scale, predicted)
        unsafe_scale = int(profile.get("unsafe_scale", 0))
        if unsafe_scale > 0:
            target = min(target, unsafe_scale - 1)
        if system_gpu_pct is not None and system_gpu_pct >= self.config.up_gpu_limit_pct:
            max_step = GPU_SATURATED_SOFT_CAP_STEP if system_gpu_pct >= 98.0 else GPU_SOFT_CAP_STEP
            target = min(target, self.current_scale + max_step)
        if target <= self.current_scale:
            return None
        confidence = VrcResolutionProfileStore.confidence(profile) * 100.0
        return Decision(
            "up",
            target,
            f"VRC 世界/人数档案建议已验证比例 {safe_scale}%（置信度 {confidence:.0f}%）",
        )

    def _cap_up_with_vrc_profile(
        self,
        decision: Decision,
        profile: dict[str, object] | None,
    ) -> Decision:
        if decision.action != "up" or profile is None:
            return decision
        unsafe_scale = int(profile.get("unsafe_scale", 0))
        if unsafe_scale <= 0 or decision.proposed_scale < unsafe_scale:
            return decision
        target = max(self.current_scale, unsafe_scale - 1)
        if target <= self.current_scale:
            return Decision("hold", self.current_scale, f"VRC 档案已记录 {unsafe_scale}% 出现压力，禁止继续升档")
        return Decision("up", target, f"{decision.reason}；受 VRC 历史压力上限 {unsafe_scale}% 限制")

    def _write_scale(
        self,
        target: int,
        reason: str,
        action: str = "manual",
        stats: WindowStats | None = None,
        system_gpu_pct: float | None = None,
    ) -> None:
        if not self.current_app:
            raise RuntimeError("当前没有场景应用")
        target = max(20, min(500, int(target)))
        old = self.current_scale
        if target == old:
            return
        self.session.set_scale(self.current_app, target)
        self.current_scale = target
        # Do not let frames rendered at the previous scale drive several more
        # high-frequency decisions after a write.
        self.history.clear()
        self.last_write = time.monotonic()
        self.write_count += 1
        self.write_observations.append(
            WriteObservation(
                write_id=self.write_count,
                app_key=self.current_app,
                action=action,
                reason=reason,
                from_scale=old,
                to_scale=target,
                started_at=self.last_write,
                pre_gpu_ms=stats.gpu_p95_ms if stats is not None else 0.0,
                pre_frame_ms=stats.interval_p95_ms if stats is not None else 0.0,
                pre_gpu_pct=system_gpu_pct,
            )
        )
        self.emit("write", f"{self.current_app}: {old}% → {target}% · {reason}")

    def _update_write_observations(
        self,
        stats: WindowStats,
        system_gpu_pct: float | None,
        now: float,
    ) -> None:
        for observation in self.write_observations:
            was_complete = observation.complete
            observation.update(stats, system_gpu_pct, now)
            if observation.complete and not was_complete:
                gpu_pct = "n/a" if observation.peak_gpu_pct is None else f"{observation.peak_gpu_pct:.0f}%"
                rollback = " · 已回退" if observation.rolled_back else ""
                self.emit(
                    "info",
                    f"写入 #{observation.write_id} 恢复窗口完成 · {observation.from_scale}%→{observation.to_scale}% · "
                    f"GPU {observation.peak_gpu_ms:.2f} ms · 帧间隔 {observation.peak_frame_ms:.2f} ms · "
                    f"GPU占用 {gpu_pct} · 重投影 {observation.peak_reprojection_pct:.1f}%{rollback}",
                )

    def _target_change_raise_decision(
        self,
        stats: WindowStats,
        target_fps: float,
        budget_ms: float,
        system_gpu_pct: float | None,
        now: float,
    ) -> Decision | None:
        pending = self.pending_target_raise
        if pending is None:
            return None
        if (
            not self.config.armed
            or self.config.mode != "continuous"
            or abs(float(pending["target_fps"]) - target_fps) > 0.1
        ):
            self.pending_target_raise = None
            return None
        if now - float(pending["requested_at"]) > TARGET_CHANGE_TIMEOUT_SECONDS:
            self.emit("warning", "未在限定时间内检测到新的实际帧率节拍；取消预测高位升档")
            self.pending_target_raise = None
            return None
        if now < self.up_blocked_until:
            pending["matched_since"] = None
            return None

        # The panel target is a scheduler budget, not a frame limiter. Wait
        # until OpenVR reports a client interval close to the new cadence so a
        # transient high-rate window cannot produce an unsafe prediction.
        cadence_matches = (
            budget_ms * 0.75 <= stats.interval_p95_ms <= budget_ms * 1.35
            and stats.gpu_p95_ms < budget_ms * self.config.gpu_down_ratio
            and stats.reprojection_pct < 3.0
            and stats.dropped == 0
            and stats.mispresented == 0
        )
        if not cadence_matches:
            pending["matched_since"] = None
            return None
        matched_since = pending["matched_since"]
        if matched_since is None:
            pending["matched_since"] = now
            return None
        if now - float(matched_since) < TARGET_CHANGE_CADENCE_SECONDS:
            return None

        predicted = math.floor(
            self.current_scale
            * budget_ms
            * TARGET_CHANGE_PROBE_RATIO
            / max(stats.gpu_p95_ms, 0.1)
        )
        target = min(self.config.max_scale, TARGET_CHANGE_PROBE_MAX_SCALE, predicted)
        gpu_soft_limited = system_gpu_pct is not None and system_gpu_pct >= self.config.up_gpu_limit_pct
        if gpu_soft_limited:
            max_step = GPU_SATURATED_SOFT_CAP_STEP if system_gpu_pct >= 98.0 else GPU_SOFT_CAP_STEP
            target = min(target, self.current_scale + max_step)
        if target <= self.current_scale:
            self.emit("info", "新节拍已稳定，但预测结果没有可用升档余量")
            self.pending_target_raise = None
            return None
        reason = (
            f"检测到 {target_fps:g} FPS 新节拍；按 GPU 帧时间预测高位升档"
            f"（最高 {TARGET_CHANGE_PROBE_MAX_SCALE}%）"
        )
        if gpu_soft_limited:
            reason += f"；系统 GPU {system_gpu_pct:.0f}% 时限制升幅"
        return Decision("up", target, reason)

    def _rollback_decision(self, stats: WindowStats, budget_ms: float, now: float) -> Decision | None:
        if not self.write_observations:
            return None
        observation = self.write_observations[-1]
        if observation.action != "up" or observation.complete or observation.to_scale != self.current_scale:
            return None
        elapsed = now - observation.started_at
        if elapsed > self.config.up_observation_seconds:
            return None
        pressure = (
            stats.gpu_p95_ms >= budget_ms * self.config.gpu_down_ratio
            or stats.reprojection_pct >= 3.0
            or stats.dropped > 0
            or stats.mispresented > 0
        )
        if not pressure:
            return None
        observation.rolled_back = True
        observation.complete = True
        self.up_blocked_until = now + self.config.up_rollback_cooldown_seconds
        return Decision(
            "rollback",
            observation.from_scale,
            f"升档后 {elapsed:.1f} 秒内检测到压力，立即回退并暂停升档",
        )

    def manual_set_scale(self, target: int) -> None:
        if not self.config.armed:
            raise PermissionError("写入锁尚未解锁")
        self._write_scale(target, "手动应用")

    def experiment_set_scale(self, target: int, reason: str) -> None:
        if not self.config.armed:
            raise PermissionError("A/B 测试需要先解锁写入")
        self._write_scale(target, reason, "experiment_setup")

    def restore_current(self) -> None:
        if not self.current_app:
            raise RuntimeError("当前没有场景应用")
        if self.current_app not in self.original_scales:
            raise RuntimeError("没有可恢复的启动值")
        self._write_scale(self.original_scales[self.current_app], "恢复启动值")

    def restore_all(self) -> None:
        for app_key, scale in self.original_scales.items():
            try:
                current, _ = self.session.get_scale(app_key)
                if current != scale:
                    self.session.set_scale(app_key, scale)
                    self.emit("write", f"{app_key}: 已恢复 {scale}%")
                    if app_key == self.current_app:
                        self.current_scale = scale
            except Exception as exc:
                self.emit("error", f"恢复 {app_key} 失败: {exc}")

    def poll(self, now: float | None = None) -> TelemetrySnapshot | None:
        if not self.connected:
            self.connect()
        if not process_running("vrserver.exe"):
            self.disconnect()
            raise RuntimeError("SteamVR 已退出")

        now = time.monotonic() if now is None else now
        pid, app_key = self.session.scene_application()
        if app_key != self.current_app:
            self._change_app(pid, app_key)
        self._update_vrc_context(app_key, now)
        if not app_key:
            return None

        for frame in self.session.frame_batch(128):
            if frame.index > self.last_frame:
                self.history.append(frame)
                self.last_frame = frame.index

        if now - self.last_eval < self.config.evaluate_seconds:
            return None
        self.last_eval = now
        if not self.history:
            if not self._waiting_reported:
                self.emit("warning", "尚未取得帧时序；等待场景应用提交画面")
                self._waiting_reported = True
            return None
        self._waiting_reported = False

        latest_ts = self.history[-1].timestamp
        window = [x for x in self.history if latest_ts - x.timestamp <= self.config.window_seconds]
        if len(window) < 10:
            return None

        observed_scale, explicitly_set = self.session.get_scale(app_key)
        if explicitly_set and observed_scale != self.current_scale:
            self.emit("info", f"检测到外部设置变化: {self.current_scale}% → {observed_scale}%")
            self.current_scale = observed_scale
            self.controller.reset()

        stats = summarize(window)
        refresh_hz = self.session.refresh_rate()
        target_fps, budget_ms = effective_frame_budget(
            refresh_hz,
            self.config.target_fps,
            self.config.target_divisor,
        )
        system_gpu_pct = self.gpu_sampler.sample(now)
        cadence_key = vrc_target_key(self.config.target_divisor, target_fps)
        vrc_profile: dict[str, object] | None = None
        vrc_profile_key = ""
        if (
            self.hardware_context is not None
            and self.vrc_context.ready
            and self.vrc_context.world_id
            and self.vrc_context.population_bucket
        ):
            vrc_profile_key = self.vrc_profile_store.profile_key(
                self.vrc_context.world_id,
                self.vrc_context.population_bucket,
                cadence_key,
            )
            vrc_profile = self.vrc_profile_store.observe(
                self.hardware_context.hardware_id,
                self.vrc_context,
                cadence_key,
                self.current_scale,
                stats.gpu_p95_ms,
                stats.cpu_p95_ms,
                budget_ms,
                stats.reprojection_pct,
                stats.dropped,
                stats.mispresented,
                now,
            )
        self._update_write_observations(stats, system_gpu_pct, now)
        rollback = self._rollback_decision(stats, budget_ms, now)
        target_change_decision = None if rollback is not None else self._target_change_raise_decision(
            stats,
            target_fps,
            budget_ms,
            system_gpu_pct,
            now,
        )
        vrc_profile_decision = (
            None
            if rollback is not None or target_change_decision is not None
            else self._vrc_profile_raise_decision(
                vrc_profile,
                vrc_profile_key,
                stats,
                budget_ms,
                system_gpu_pct,
                now,
            )
        )
        if rollback is not None:
            decision_source = "rollback"
            decision = rollback
        elif target_change_decision is not None:
            decision_source = "target_change"
            decision = target_change_decision
        elif vrc_profile_decision is not None:
            decision_source = "vrc_profile"
            decision = vrc_profile_decision
        else:
            decision_source = "controller"
            decision = self.controller.decide(
                stats,
                budget_ms,
                self.current_scale,
                now,
                self.config,
                system_gpu_pct,
            )
        decision = self._cap_up_with_vrc_profile(decision, vrc_profile)
        if decision.action == "up" and now < self.up_blocked_until:
            remaining = self.up_blocked_until - now
            decision = Decision("hold", self.current_scale, f"升档回退冷却中，还剩 {remaining:.0f} 秒")
        write_applied = False
        write_cooldown = (
            0.0
            if decision_source in {"target_change", "vrc_profile"}
            else action_cooldown_seconds(decision.action, self.config)
        )
        may_write = (
            self.config.armed
            and self.config.mode in {"one_step", "continuous"}
            and decision.action in {"up", "down", "rollback"}
            and decision.proposed_scale != self.current_scale
            and now - self.last_write >= write_cooldown
            and (self.config.mode == "continuous" or not self.one_step_written)
        )
        if may_write:
            self._write_scale(
                decision.proposed_scale,
                decision.reason,
                decision.action,
                stats,
                system_gpu_pct,
            )
            if decision_source == "target_change":
                self.pending_target_raise = None
            elif decision_source == "vrc_profile":
                self.vrc_profile_applied_key = vrc_profile_key
            self.one_step_written = True
            write_applied = True
        elif decision.action == "hold" and decision_source == "target_change":
            self.pending_target_raise = None

        width, height = self.session.recommended_size()
        self.sample_seq += 1
        observation = self.write_observations[-1] if self.write_observations else None
        recovery_elapsed = min(WRITE_RECOVERY_SECONDS, max(0.0, now - observation.started_at)) if observation else 0.0
        if observation is not None and not observation.complete:
            observation_phase = f"post_{observation.action}"
        elif self.controller.stable_since is not None:
            observation_phase = "pre_up_stable"
        else:
            observation_phase = "steady"
        return TelemetrySnapshot(
            local_time=dt.datetime.now(),
            app_key=app_key,
            app_pid=pid,
            refresh_hz=refresh_hz,
            target_divisor=self.config.target_divisor,
            target_fps=target_fps,
            budget_ms=budget_ms,
            render_width=width,
            render_height=height,
            resolution_scale=self.current_scale,
            gpu_p50_ms=stats.gpu_p50_ms,
            gpu_p95_ms=stats.gpu_p95_ms,
            cpu_p95_ms=stats.cpu_p95_ms,
            frame_interval_p95_ms=stats.interval_p95_ms,
            reprojection_pct=stats.reprojection_pct,
            dropped=stats.dropped,
            mispresented=stats.mispresented,
            system_cpu_pct=psutil.cpu_percent(interval=None),
            system_gpu_pct=system_gpu_pct,
            vrc_world_id=self.vrc_context.world_id,
            vrc_world_short=self.vrc_context.world_short,
            vrc_population=self.vrc_context.population,
            vrc_population_bucket=self.vrc_context.population_bucket,
            vrc_context_ready=self.vrc_context.ready,
            vrc_recent_joins=self.vrc_context.recent_joins,
            vrc_recent_leaves=self.vrc_context.recent_leaves,
            vrc_population_delta_10s=self.vrc_context.population_delta_10s,
            vrc_population_delta_60s=self.vrc_context.population_delta_60s,
            vrc_seconds_since_population_change=self.vrc_context.seconds_since_population_change,
            vrc_profile_safe_scale=int(vrc_profile.get("safe_scale", 0)) if vrc_profile else 0,
            vrc_profile_unsafe_scale=int(vrc_profile.get("unsafe_scale", 0)) if vrc_profile else 0,
            vrc_profile_samples=int(vrc_profile.get("samples", 0)) if vrc_profile else 0,
            vrc_profile_confidence=VrcResolutionProfileStore.confidence(vrc_profile),
            decision=decision,
            write_applied=write_applied,
            write_count=self.write_count,
            sample_seq=self.sample_seq,
            observation_phase=observation_phase,
            write_id=observation.write_id if observation else 0,
            write_from_scale=observation.from_scale if observation else self.current_scale,
            write_to_scale=observation.to_scale if observation else self.current_scale,
            write_action=observation.action if observation else "",
            write_reason=observation.reason if observation else "",
            write_pre_gpu_ms=observation.pre_gpu_ms if observation else 0.0,
            write_pre_frame_ms=observation.pre_frame_ms if observation else 0.0,
            recovery_elapsed_s=recovery_elapsed,
            recovery_complete=observation.complete if observation else False,
            recovery_rolled_back=observation.rolled_back if observation else False,
            recovery_peak_gpu_ms=observation.peak_gpu_ms if observation else 0.0,
            recovery_peak_frame_ms=observation.peak_frame_ms if observation else 0.0,
            recovery_peak_gpu_pct=observation.peak_gpu_pct if observation else None,
            recovery_peak_reprojection_pct=observation.peak_reprojection_pct if observation else 0.0,
            recovery_dropped=observation.dropped if observation else 0,
            recovery_mispresented=observation.mispresented if observation else 0,
        )


def _round_scale(value: float) -> int:
    return max(20, min(500, int(round(value / 5.0) * 5)))


def calculate_calibration(
    context: HardwareContext,
    app_key: str,
    original_scale: int,
    budget_ms: float,
    samples_by_scale: dict[int, list[TelemetrySnapshot | dict[str, object]]],
    precise: bool,
) -> CalibrationResult:
    """Build a machine-local recommendation from read-only or stepped samples."""
    aggregates: dict[str, dict[str, float]] = {}
    points: list[tuple[float, float]] = []
    all_gpu: list[float] = []
    all_cpu: list[float] = []
    for scale, samples in sorted(samples_by_scale.items()):
        gpu_values: list[float] = []
        cpu_values: list[float] = []
        for sample in samples:
            if isinstance(sample, TelemetrySnapshot):
                gpu_values.append(float(sample.gpu_p95_ms))
                cpu_values.append(float(sample.cpu_p95_ms))
            else:
                gpu_values.append(float(sample["gpu_p95_ms"]))
                cpu_values.append(float(sample["cpu_p95_ms"]))
        if not gpu_values:
            continue
        gpu_median = statistics.median(gpu_values)
        cpu_median = statistics.median(cpu_values)
        aggregates[str(scale)] = {
            "count": float(len(gpu_values)),
            "gpu_p95_median_ms": round(gpu_median, 4),
            "cpu_p95_median_ms": round(cpu_median, 4),
        }
        points.append((float(scale), gpu_median))
        all_gpu.extend(gpu_values)
        all_cpu.extend(cpu_values)
    if not points:
        raise ValueError("校准没有取得有效帧时序样本")

    gpu_median = statistics.median(all_gpu)
    cpu_median = statistics.median(all_cpu)
    cpu_bound = cpu_median >= budget_ms * 0.92 and gpu_median < budget_ms * 0.80
    target_gpu_ms = budget_ms * 0.85
    recommended = float(original_scale)

    if not cpu_bound:
        if precise and len(points) >= 2:
            x_mean = statistics.mean(x for x, _ in points)
            y_mean = statistics.mean(y for _, y in points)
            denominator = sum((x - x_mean) ** 2 for x, _ in points)
            slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator if denominator else 0.0
            intercept = y_mean - slope * x_mean
            if slope > 0.005:
                recommended = (target_gpu_ms - intercept) / slope
            elif gpu_median > 0.1:
                recommended = original_scale * target_gpu_ms / gpu_median
        elif gpu_median > 0.1:
            # SteamVR resolutionScale approximates total pixel workload, so a
            # proportional estimate is a reasonable read-only starting point.
            recommended = original_scale * target_gpu_ms / gpu_median

    recommended_scale = _round_scale(recommended)
    return CalibrationResult(
        app_key=app_key,
        hardware_id=context.hardware_id,
        precise=precise,
        original_scale=int(original_scale),
        recommended_scale=recommended_scale,
        recommended_min=max(20, recommended_scale - 15),
        recommended_max=min(500, recommended_scale + 30),
        gpu_budget_ms=round(float(budget_ms), 4),
        samples=aggregates,
        cpu_bound=cpu_bound,
        created_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


def run_self_test() -> int:
    controller = AdaptiveController()
    config = RuntimeConfig(
        min_scale=40,
        max_scale=150,
        step_down=1,
        step_up=5,
        raise_stable_seconds=4.0,
        target_divisor=2,
    )
    base = WindowStats(180, 7.0, 12.0, 5.0, 11.0, 5.0, 1, 0)
    assert controller.decide(base, 11.111, 100, 0.0, config).proposed_scale == 99
    below_min_overloaded = AdaptiveController().decide(base, 11.111, 20, 0.0, config)
    assert below_min_overloaded.action == "hold" and below_min_overloaded.proposed_scale == 20

    cpu_bound = WindowStats(180, 4.0, 5.0, 12.0, 11.0, 0.0, 0, 0)
    assert controller.decide(cpu_bound, 11.111, 100, 1.0, config).action == "hold"

    stable = WindowStats(180, 4.0, 6.0, 7.0, 11.0, 0.0, 0, 0)
    assert controller.decide(stable, 11.111, 100, 2.0, config).action == "hold"
    predicted_up = controller.decide(stable, 11.111, 100, 7.0, config)
    assert predicted_up.action == "up" and predicted_up.proposed_scale == 133

    budget_30fps = 1000.0 / 30.0
    mild_over = WindowStats(180, 25.0, 31.0, 8.0, 33.0, 0.0, 0, 0)
    assert AdaptiveController().decide(mild_over, budget_30fps, 120, 1.0, config).proposed_scale == 119
    ample_30fps = WindowStats(180, 16.0, 20.0, 8.0, 33.0, 0.0, 0, 0)
    predicted_30fps = AdaptiveController().decide(
        ample_30fps,
        budget_30fps,
        100,
        1.0,
        replace(config, raise_stable_seconds=0.0),
    )
    assert predicted_30fps.action == "up" and 119 <= predicted_30fps.proposed_scale <= 120
    assert action_cooldown_seconds("down", config) == 0.25
    assert action_cooldown_seconds("up", config) == config.cooldown_seconds
    assert action_cooldown_seconds("rollback", config) == 0.0

    observation = WriteObservation(
        1,
        "steam.app.test",
        "up",
        "test",
        100,
        120,
        0.0,
        20.0,
        25.0,
        80.0,
    )
    observation.update(mild_over, 96.0, WRITE_RECOVERY_SECONDS)
    assert observation.complete and observation.peak_gpu_ms == 31.0 and observation.peak_gpu_pct == 96.0
    rollback_runtime = AdaptiveRuntime(replace(config, up_observation_seconds=2.0))
    rollback_runtime.current_scale = 120
    rollback_runtime.write_observations.append(
        WriteObservation(1, "steam.app.test", "up", "test", 100, 120, 0.0, 20.0, 25.0, 80.0)
    )
    rollback = rollback_runtime._rollback_decision(mild_over, budget_30fps, 1.0)
    assert rollback is not None and rollback.action == "rollback" and rollback.proposed_scale == 100

    capped = RuntimeConfig(min_scale=40, max_scale=100, raise_stable_seconds=0)
    assert controller.decide(stable, 11.111, 100, 8.0, capped).proposed_scale == 100
    below_min_stable = AdaptiveController().decide(stable, 11.111, 20, 8.0, capped)
    assert below_min_stable.proposed_scale == 40
    saturated = AdaptiveController().decide(
        stable,
        11.111,
        80,
        8.0,
        replace(capped, max_scale=150, raise_stable_seconds=0.0),
        99.0,
    )
    assert saturated.action == "up" and saturated.proposed_scale == 85

    target_change_runtime = AdaptiveRuntime(
        RuntimeConfig(mode="continuous", armed=True, target_divisor=1, max_scale=200)
    )
    target_change_runtime.hardware_context = HardwareContext(
        "test",
        "machine",
        "gpu",
        "hmd-maker",
        "hmd-model",
        90.0,
        2000,
        2000,
    )
    target_change_runtime.current_scale = 100
    target_change_runtime.update_config(
        replace(target_change_runtime.config, target_divisor=2)
    )
    assert target_change_runtime.pending_target_raise is not None
    cadence = WindowStats(180, 10.0, 12.0, 8.0, 22.0, 0.0, 0, 0)
    requested = float(target_change_runtime.pending_target_raise["requested_at"])
    assert target_change_runtime._target_change_raise_decision(
        cadence, 45.0, 1000.0 / 45.0, 80.0, requested + 0.1
    ) is None
    target_probe = target_change_runtime._target_change_raise_decision(
        cadence, 45.0, 1000.0 / 45.0, 80.0, requested + 1.0
    )
    assert target_probe is not None and target_probe.action == "up" and target_probe.proposed_scale == 150
    target_change_runtime.pending_target_raise["matched_since"] = requested + 0.1
    target_probe_saturated = target_change_runtime._target_change_raise_decision(
        cadence, 45.0, 1000.0 / 45.0, 95.0, requested + 1.0
    )
    assert target_probe_saturated is not None and target_probe_saturated.proposed_scale == 110

    assert effective_frame_budget(72.0, target_divisor=1) == (72.0, 1000.0 / 72.0)
    assert effective_frame_budget(72.0, target_divisor=2) == (36.0, 1000.0 / 36.0)
    assert effective_frame_budget(72.0, target_divisor=3) == (24.0, 1000.0 / 24.0)
    assert effective_frame_budget(72.0, target_divisor=4) == (18.0, 1000.0 / 18.0)
    assert effective_frame_budget(90.0, 60.0, 0) == (60.0, 1000.0 / 60.0)

    exported = portable_policy(config, "self-test")
    imported = config_from_portable_policy(exported, RuntimeConfig(mode="continuous", armed=True))
    assert imported.mode == "monitor" and not imported.armed
    assert imported.step_down == config.step_down and imported.gpu_down_ratio == config.gpu_down_ratio
    assert imported.target_divisor == 2 and imported.target_fps == 0.0
    legacy = portable_policy(replace(config, target_divisor=0, target_fps=30.0), "legacy")
    del legacy["policy"]["target_divisor"]  # type: ignore[index]
    legacy_import = config_from_portable_policy(legacy)
    assert legacy_import.target_divisor == 0 and legacy_import.target_fps == 30.0

    with tempfile.TemporaryDirectory(prefix="vrc-context-selftest-") as temporary:
        temporary_root = Path(temporary)
        log_path = temporary_root / "output_log_2026-01-01_00-00-00.txt"
        log_path.write_text(
            "\n".join(
                (
                    "[Behaviour] Joining wrld_11111111-1111-1111-1111-111111111111:123~private(redacted)",
                    "[Behaviour] OnPlayerJoined local-user",
                    "[Behaviour] OnPlayerJoined existing-user-1",
                    "[Behaviour] OnPlayerJoined existing-user-2",
                    "",
                )
            ),
            encoding="utf-8",
        )
        provider = VrcLogContextProvider(temporary_root)
        initializing = provider.poll(100.0)
        assert initializing.population == 3 and not initializing.ready
        ready_context = provider.poll(102.0)
        assert ready_context.ready and ready_context.population == 3
        with log_path.open("a", encoding="utf-8") as output:
            output.write("[Behaviour] OnPlayerLeft existing-user-1\n")
            output.write("[Behaviour] OnPlayerJoined later-user\n")
        updated_context = provider.poll(103.0)
        assert updated_context.population == 3 and not updated_context.ready
        assert updated_context.recent_leaves == 1
        assert updated_context.population_delta_60s == 3

        profile_path = temporary_root / "profiles.json"
        profile_store = VrcResolutionProfileStore(profile_path)
        learned_context = VrcContextSnapshot(
            world_id="wrld_11111111-1111-1111-1111-111111111111",
            population=12,
            population_bucket="11-20",
            ready=True,
            joined_at=1.0,
            last_population_change_at=1.0,
        )
        learned_profile = None
        for index in range(10):
            learned_profile = profile_store.observe(
                "hardware",
                learned_context,
                "d2",
                120,
                10.0,
                8.0,
                20.0,
                0.0,
                0,
                0,
                200.0 + index,
            )
        assert learned_profile is not None
        assert int(learned_profile["safe_scale"]) == 120
        assert int(learned_profile["safe_evidence"]) >= 8
        for index in range(3):
            learned_profile = profile_store.observe(
                "hardware",
                learned_context,
                "d2",
                140,
                19.0,
                8.0,
                20.0,
                0.0,
                0,
                0,
                220.0 + index,
            )
        assert learned_profile is not None and int(learned_profile["unsafe_scale"]) == 140
        profile_store.save_if_due(230.0, force=True)
        reloaded_profile = VrcResolutionProfileStore(profile_path).get(
            "hardware",
            learned_context.world_id,
            learned_context.population_bucket,
            "d2",
        )
        assert reloaded_profile is not None and int(reloaded_profile["safe_scale"]) == 120
        profile_runtime = AdaptiveRuntime(
            RuntimeConfig(mode="continuous", armed=True, max_scale=200)
        )
        profile_runtime.current_scale = 100
        profile_runtime.vrc_context = learned_context
        profile_runtime.vrc_context_stable_since = 1.0
        profile_decision = profile_runtime._vrc_profile_raise_decision(
            reloaded_profile,
            "profile-key",
            WindowStats(180, 8.0, 10.0, 8.0, 19.0, 0.0, 0, 0),
            20.0,
            80.0,
            10.0,
        )
        assert profile_decision is not None and profile_decision.proposed_scale == 120
        capped_profile_decision = profile_runtime._cap_up_with_vrc_profile(
            Decision("up", 160, "test"),
            reloaded_profile,
        )
        assert capped_profile_decision.proposed_scale == 139

        collector = PassiveVrcDataCollector(
            temporary_root / "shared",
            sample_interval_seconds=0.1,
            world_warmup_seconds=0.0,
            steady_quiet_seconds=0.0,
            steady_window_seconds=2.0,
            transition_pre_seconds=2.0,
            transition_post_seconds=2.0,
        )
        hardware = {
            "machine_name": "must-not-export",
            "hardware_id": "must-not-export",
            "gpu_name": "test-gpu",
            "gpu_vram_mib": 12288,
            "gpu_count": 1,
            "cpu_name": "test-cpu",
            "cpu_physical_cores": 8,
            "cpu_logical_cores": 16,
            "system_ram_mib": 32768,
            "hmd_manufacturer": "test-maker",
            "hmd_model": "test-hmd",
        }

        def passive_sample(population: int, quiet: float) -> dict[str, object]:
            return {
                "app_key": "steam.app.438100",
                "vrc_world_id": "wrld_11111111-1111-1111-1111-111111111111",
                "vrc_population": population,
                "vrc_population_bucket": "1-5",
                "vrc_context_ready": True,
                "vrc_seconds_since_population_change": quiet,
                "refresh_hz": 90.0,
                "target_divisor": 2,
                "target_fps": 45.0,
                "budget_ms": 1000.0 / 45.0,
                "render_width": 2000,
                "render_height": 2000,
                "resolution_scale": 100,
                "gpu_p50_ms": 7.0,
                "gpu_p95_ms": 8.0,
                "cpu_p95_ms": 6.0,
                "frame_interval_p95_ms": 22.0,
                "reprojection_pct": 0.0,
                "dropped": 0,
                "mispresented": 0,
                "system_cpu_pct": 25.0,
                "system_gpu_pct": 70.0,
            }

        for tick in range(5):
            collector.observe(passive_sample(3, 30.0), hardware, tick * 0.5)
        assert collector.status()["steady_records"] == 1
        for tick in range(5, 10):
            population = 4
            collector.observe(
                passive_sample(population, max(0.0, (tick - 5) * 0.5)),
                hardware,
                tick * 0.5,
            )
        assert collector.status()["transition_records"] == 1
        exported_path = collector.export_share_package(temporary_root / "share.zip")
        assert exported_path.exists()
        with zipfile.ZipFile(exported_path) as archive:
            records_payload = archive.read("records.jsonl").decode("utf-8")
            assert "must-not-export" not in records_payload
            assert "population_transition" in records_payload
            assert '"cpu_name":"test-cpu"' in records_payload
            assert '"gpu_vram_mib":12288' in records_payload
            assert '"system_ram_mib":32768' in records_payload
        upload_calls: list[tuple[str, int]] = []

        def fake_upload(endpoint: str, lines: list[bytes]) -> dict[str, object]:
            upload_calls.append((endpoint, len(lines)))
            return {
                "ok": True,
                "batch_id": f"batch-{len(upload_calls)}",
                "accepted_records": len(lines),
                "duplicate_records": 0,
            }

        collector._upload_archive = fake_upload  # type: ignore[method-assign]
        first_upload = collector.upload_pending("https://example.invalid/")
        assert first_upload["batches"] == 1
        assert first_upload["accepted_records"] == 2
        assert upload_calls == [("https://example.invalid", 2)]
        second_upload = collector.upload_pending("https://example.invalid")
        assert second_upload["batches"] == 0
        assert len(upload_calls) == 1

    context = HardwareContext("test", "pc", "gpu", "maker", "hmd", 90.0, 2000, 2000)
    result = calculate_calibration(
        context,
        "steam.app.test",
        100,
        11.111,
        {
            90: [{"gpu_p95_ms": 7.8, "cpu_p95_ms": 4.0}],
            100: [{"gpu_p95_ms": 8.6, "cpu_p95_ms": 4.2}],
            110: [{"gpu_p95_ms": 9.4, "cpu_p95_ms": 4.4}],
        },
        precise=True,
    )
    assert 105 <= result.recommended_scale <= 115 and not result.cpu_bound
    print("SELF-TEST PASS: 控制核心、VRC 被动数据采集、目标 FPS 预算与校准计算正常。")
    return 0
