from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile


WORLD_PATTERN = re.compile(
    r"\[Behaviour\]\s+Joining\s+(wrld_[0-9a-fA-F-]+)(?::|\s|$)"
)
PLAYER_JOIN_PATTERN = re.compile(r"\[Behaviour\]\s+OnPlayerJoined\s+(.+?)\s*$")
PLAYER_LEFT_PATTERN = re.compile(r"\[Behaviour\]\s+OnPlayerLeft\s+(.+?)\s*$")
INITIAL_POPULATION_QUIET_SECONDS = 1.5
LOG_DISCOVERY_INTERVAL_SECONDS = 1.0
PROFILE_SAVE_INTERVAL_SECONDS = 5.0
SAFE_STREAK_SAMPLES = 8
PRESSURE_STREAK_SAMPLES = 3
PROFILE_SCHEMA_VERSION = 2


def population_bucket(population: int) -> str:
    population = max(0, int(population))
    if population <= 5:
        return "1-5"
    if population <= 10:
        return "6-10"
    if population <= 20:
        return "11-20"
    if population <= 40:
        return "21-40"
    return "41+"


def target_key(target_divisor: int, target_fps: float) -> str:
    if target_divisor in {1, 2, 3, 4}:
        return f"d{target_divisor}"
    return f"fps{target_fps:g}"


@dataclass(frozen=True)
class VrcContextSnapshot:
    world_id: str = ""
    population: int = 0
    population_bucket: str = ""
    ready: bool = False
    joined_at: float = 0.0
    last_population_change_at: float = 0.0
    recent_joins: int = 0
    recent_leaves: int = 0
    population_delta_10s: int = 0
    population_delta_60s: int = 0
    seconds_since_population_change: float = 0.0

    @property
    def world_short(self) -> str:
        if not self.world_id:
            return ""
        return self.world_id.removeprefix("wrld_")[:8]


class VrcLogContextProvider:
    """Tails VRChat's local log without retaining player or instance IDs."""

    def __init__(self, log_dir: Path | None = None) -> None:
        if log_dir is None:
            user_root = Path(os.environ.get("USERPROFILE", str(Path.home())))
            log_dir = user_root / "AppData" / "LocalLow" / "VRChat" / "VRChat"
        self.log_dir = log_dir
        self.log_path: Path | None = None
        self.offset = 0
        self.partial = b""
        self.world_id = ""
        self.player_hashes: set[str] = set()
        self.joined_at = 0.0
        self.last_population_change_at = 0.0
        self.join_times: list[float] = []
        self.leave_times: list[float] = []
        self.last_discovery_at = -1e9

    @staticmethod
    def _player_hash(value: str) -> str:
        return hashlib.sha256(value.strip().encode("utf-8", errors="replace")).hexdigest()[:20]

    def _latest_log(self) -> Path | None:
        if not self.log_dir.exists():
            return None
        try:
            return max(
                self.log_dir.glob("output_log_*.txt"),
                key=lambda path: path.stat().st_mtime_ns,
                default=None,
            )
        except OSError:
            return None

    def _reset_for_log(self, path: Path) -> None:
        self.log_path = path
        self.offset = 0
        self.partial = b""
        self.world_id = ""
        self.player_hashes.clear()
        self.joined_at = 0.0
        self.last_population_change_at = 0.0
        self.join_times.clear()
        self.leave_times.clear()

    def _process_line(self, line: str, now: float) -> None:
        world_match = WORLD_PATTERN.search(line)
        if world_match:
            self.world_id = world_match.group(1)
            self.player_hashes.clear()
            self.joined_at = now
            self.last_population_change_at = now
            self.join_times.clear()
            self.leave_times.clear()
            return
        if not self.world_id:
            return
        joined = PLAYER_JOIN_PATTERN.search(line)
        if joined:
            player = self._player_hash(joined.group(1))
            before = len(self.player_hashes)
            self.player_hashes.add(player)
            if len(self.player_hashes) != before:
                self.last_population_change_at = now
                self.join_times.append(now)
            return
        left = PLAYER_LEFT_PATTERN.search(line)
        if left:
            player = self._player_hash(left.group(1))
            if player in self.player_hashes:
                self.player_hashes.remove(player)
                self.last_population_change_at = now
                self.leave_times.append(now)

    def poll(self, now: float | None = None) -> VrcContextSnapshot:
        now = time.monotonic() if now is None else now
        if now - self.last_discovery_at >= LOG_DISCOVERY_INTERVAL_SECONDS:
            self.last_discovery_at = now
            latest = self._latest_log()
            if latest is not None and latest != self.log_path:
                self._reset_for_log(latest)
        if self.log_path is not None:
            try:
                size = self.log_path.stat().st_size
                if size < self.offset:
                    self._reset_for_log(self.log_path)
                with self.log_path.open("rb") as stream:
                    stream.seek(self.offset)
                    chunk = stream.read()
                    self.offset = stream.tell()
                if chunk:
                    payload = self.partial + chunk
                    lines = payload.split(b"\n")
                    self.partial = lines.pop() if payload and not payload.endswith(b"\n") else b""
                    for raw_line in lines:
                        self._process_line(raw_line.decode("utf-8", errors="replace").rstrip("\r"), now)
            except OSError:
                pass
        cutoff = now - 60.0
        self.join_times = [joined for joined in self.join_times if joined >= cutoff]
        self.leave_times = [left for left in self.leave_times if left >= cutoff]
        population = len(self.player_hashes)
        ready = bool(
            self.world_id
            and population > 0
            and now - self.last_population_change_at >= INITIAL_POPULATION_QUIET_SECONDS
        )
        return VrcContextSnapshot(
            world_id=self.world_id,
            population=population,
            population_bucket=population_bucket(population) if population > 0 else "",
            ready=ready,
            joined_at=self.joined_at,
            last_population_change_at=self.last_population_change_at,
            recent_joins=len(self.join_times),
            recent_leaves=len(self.leave_times),
            population_delta_10s=(
                sum(joined >= now - 10.0 for joined in self.join_times)
                - sum(left >= now - 10.0 for left in self.leave_times)
            ),
            population_delta_60s=len(self.join_times) - len(self.leave_times),
            seconds_since_population_change=(
                max(0.0, now - self.last_population_change_at)
                if self.last_population_change_at > 0.0
                else 0.0
            ),
        )


class VrcResolutionProfileStore:
    """Learns proven-safe and proven-unsafe scales for local VRC contexts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, object] = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profiles": {},
        }
        self.safe_streaks: dict[str, tuple[int, int]] = {}
        self.pressure_streaks: dict[str, tuple[int, int]] = {}
        self.last_save_at = -1e9
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return
            if loaded.get("schema_version") == PROFILE_SCHEMA_VERSION:
                self.data = loaded
            elif loaded.get("schema_version") == 1:
                self.data = self._migrate_v1(loaded)
                self.dirty = True
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _migrate_v1(loaded: dict[str, object]) -> dict[str, object]:
        """Quarantine unsafe evidence that could be based only on mispresented."""
        profiles = loaded.get("profiles")
        if isinstance(profiles, dict):
            for hardware in profiles.values():
                if not isinstance(hardware, dict):
                    continue
                for profile in hardware.values():
                    if not isinstance(profile, dict):
                        continue
                    unsafe_scale = int(profile.get("unsafe_scale", 0))
                    pressure_samples = int(profile.get("pressure_samples", 0))
                    if unsafe_scale > 0:
                        profile["legacy_v1_unsafe_scale"] = unsafe_scale
                    if pressure_samples > 0:
                        profile["legacy_v1_pressure_samples"] = pressure_samples
                    profile["unsafe_scale"] = 0
                    profile["pressure_samples"] = 0
        loaded["schema_version"] = PROFILE_SCHEMA_VERSION
        return loaded

    @staticmethod
    def profile_key(world_id: str, bucket: str, cadence: str) -> str:
        return f"{world_id}|{bucket}|{cadence}"

    def _hardware_profiles(self, hardware_id: str) -> dict[str, object]:
        profiles = self.data.setdefault("profiles", {})
        assert isinstance(profiles, dict)
        hardware = profiles.setdefault(hardware_id, {})
        if not isinstance(hardware, dict):
            hardware = {}
            profiles[hardware_id] = hardware
        return hardware

    def get(
        self,
        hardware_id: str,
        world_id: str,
        bucket: str,
        cadence: str,
    ) -> dict[str, object] | None:
        value = self._hardware_profiles(hardware_id).get(self.profile_key(world_id, bucket, cadence))
        return value if isinstance(value, dict) else None

    def observe(
        self,
        hardware_id: str,
        context: VrcContextSnapshot,
        cadence: str,
        scale: int,
        gpu_ms: float,
        cpu_ms: float,
        budget_ms: float,
        reprojection_pct: float,
        dropped: int,
        mispresented: int,
        now: float,
    ) -> dict[str, object] | None:
        if not context.ready or not context.world_id or not context.population_bucket:
            return None
        key = self.profile_key(context.world_id, context.population_bucket, cadence)
        hardware = self._hardware_profiles(hardware_id)
        profile = hardware.setdefault(
            key,
            {
                "world_id": context.world_id,
                "population_bucket": context.population_bucket,
                "target": cadence,
                "safe_scale": 0,
                "safe_evidence": 0,
                "unsafe_scale": 0,
                "stable_samples": 0,
                "pressure_samples": 0,
                "samples": 0,
                "updated_at": "",
            },
        )
        if not isinstance(profile, dict):
            return None
        stable = (
            gpu_ms <= budget_ms * 0.85
            and cpu_ms <= budget_ms * 0.92
            and reprojection_pct < 3.0
            and dropped == 0
        )
        pressure = (
            gpu_ms >= budget_ms * 0.92
            or reprojection_pct >= 3.0
            or dropped > 0
        )
        profile["samples"] = int(profile.get("samples", 0)) + 1
        if stable:
            profile["stable_samples"] = int(profile.get("stable_samples", 0)) + 1
            last_scale, streak = self.safe_streaks.get(key, (scale, 0))
            streak = streak + 1 if last_scale == scale else 1
            self.safe_streaks[key] = (scale, streak)
            if streak >= SAFE_STREAK_SAMPLES:
                previous_safe = int(profile.get("safe_scale", 0))
                if scale > previous_safe:
                    profile["safe_scale"] = scale
                    profile["safe_evidence"] = streak
                elif scale == previous_safe:
                    profile["safe_evidence"] = int(profile.get("safe_evidence", 0)) + 1
        else:
            self.safe_streaks.pop(key, None)
        if pressure:
            profile["pressure_samples"] = int(profile.get("pressure_samples", 0)) + 1
            last_scale, streak = self.pressure_streaks.get(key, (scale, 0))
            streak = streak + 1 if last_scale == scale else 1
            self.pressure_streaks[key] = (scale, streak)
            if streak >= PRESSURE_STREAK_SAMPLES:
                previous_unsafe = int(profile.get("unsafe_scale", 0))
                profile["unsafe_scale"] = scale if previous_unsafe <= 0 else min(previous_unsafe, scale)
        else:
            self.pressure_streaks.pop(key, None)
        unsafe = int(profile.get("unsafe_scale", 0))
        safe = int(profile.get("safe_scale", 0))
        if unsafe > 0 and safe >= unsafe:
            profile["safe_scale"] = max(0, unsafe - 1)
            profile["safe_evidence"] = 0
        profile["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.dirty = True
        self.save_if_due(now)
        return profile

    def save_if_due(self, now: float, force: bool = False) -> None:
        if not self.dirty or (not force and now - self.last_save_at < PROFILE_SAVE_INTERVAL_SECONDS):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self.last_save_at = now
            self.dirty = False
        except OSError:
            pass

    @staticmethod
    def confidence(profile: dict[str, object] | None) -> float:
        if profile is None:
            return 0.0
        return min(1.0, int(profile.get("safe_evidence", 0)) / 30.0)


class TelemetryUploadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        retry_after_seconds: int = 0,
    ) -> None:
        super().__init__(message)
        self.http_status = max(0, int(http_status))
        self.retry_after_seconds = max(0, int(retry_after_seconds))


class PassiveVrcDataCollector:
    """Builds privacy-preserving, upload-ready aggregates during normal play."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        sample_interval_seconds: float = 1.0,
        world_warmup_seconds: float = 60.0,
        steady_quiet_seconds: float = 15.0,
        steady_window_seconds: float = 60.0,
        transition_pre_seconds: float = 20.0,
        transition_post_seconds: float = 30.0,
    ) -> None:
        self.root = root
        self.records_path = root / "passive-vrc-records-v1.jsonl"
        self.identity_path = root / "anonymous-contributor.json"
        self.upload_state_path = root / "upload-state-v1.json"
        self.enabled = bool(enabled)
        self.sample_interval_seconds = max(0.05, float(sample_interval_seconds))
        self.world_warmup_seconds = max(0.0, float(world_warmup_seconds))
        self.steady_quiet_seconds = max(0.0, float(steady_quiet_seconds))
        self.steady_window_seconds = max(1.0, float(steady_window_seconds))
        self.transition_pre_seconds = max(1.0, float(transition_pre_seconds))
        self.transition_post_seconds = max(1.0, float(transition_post_seconds))
        self.contributor_id = self._load_or_create_contributor_id()
        self.session_id = uuid.uuid4().hex
        self._last_observed_at = -1e9
        self._current_world = ""
        self._world_seen_at = 0.0
        self._last_population: int | None = None
        self._last_settings_signature: tuple[object, ...] | None = None
        history_length = max(8, int(self.transition_pre_seconds / self.sample_interval_seconds) + 8)
        self._recent_samples: deque[tuple[float, dict[str, float | int | None]]] = deque(
            maxlen=history_length
        )
        self._steady_key: tuple[object, ...] | None = None
        self._steady_started_at = 0.0
        self._steady_samples: list[dict[str, float | int | None]] = []
        self._transition: dict[str, object] | None = None
        self._worlds: set[str] = set()
        self._contexts: set[str] = set()
        self._steady_records = 0
        self._transition_records = 0
        self._file_lock = threading.Lock()
        self._load_progress()

    @staticmethod
    def _value(source: object, name: str, default: object = None) -> object:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    def _load_or_create_contributor_id(self) -> str:
        try:
            data = json.loads(self.identity_path.read_text(encoding="utf-8"))
            value = data.get("anonymous_contributor_id") if isinstance(data, dict) else None
            if isinstance(value, str) and len(value) >= 16:
                return value
        except (OSError, TypeError, ValueError):
            pass
        value = uuid.uuid4().hex
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.identity_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": self.SCHEMA_VERSION,
                        "anonymous_contributor_id": value,
                        "note": "Random local identifier; no machine name or player identity is stored.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.identity_path)
        except OSError:
            pass
        return value

    def _load_progress(self) -> None:
        try:
            with self.records_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    world_id = str(record.get("world_id", ""))
                    bucket = str(record.get("population_bucket", ""))
                    if world_id:
                        self._worlds.add(world_id)
                    if world_id and bucket:
                        self._contexts.add(f"{world_id}|{bucket}")
                    record_type = str(record.get("record_type", ""))
                    if record_type == "steady_window":
                        self._steady_records += 1
                    elif record_type == "population_transition":
                        self._transition_records += 1
        except OSError:
            pass

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    @classmethod
    def _summary(
        cls, samples: list[dict[str, float | int | None]]
    ) -> dict[str, float | int | None]:
        def values(name: str) -> list[float]:
            return [float(sample[name]) for sample in samples if sample.get(name) is not None]

        def rounded(value: float) -> float:
            return round(value, 4)

        gpu_p50 = values("gpu_p50_ms")
        gpu_p95 = values("gpu_p95_ms")
        cpu_p95 = values("cpu_p95_ms")
        interval_p95 = values("frame_interval_p95_ms")
        reprojection = values("reprojection_pct")
        system_cpu = values("system_cpu_pct")
        system_gpu = values("system_gpu_pct")
        populations = values("population")
        return {
            "sample_count": len(samples),
            "population_min": int(min(populations)) if populations else 0,
            "population_max": int(max(populations)) if populations else 0,
            "population_mean": rounded(statistics.fmean(populations)) if populations else 0.0,
            "gpu_p50_ms_mean": rounded(statistics.fmean(gpu_p50)) if gpu_p50 else 0.0,
            "gpu_p95_ms_median": rounded(cls._percentile(gpu_p95, 0.50)),
            "gpu_p95_ms_p95": rounded(cls._percentile(gpu_p95, 0.95)),
            "cpu_p95_ms_median": rounded(cls._percentile(cpu_p95, 0.50)),
            "cpu_p95_ms_p95": rounded(cls._percentile(cpu_p95, 0.95)),
            "frame_interval_p95_ms_p95": rounded(cls._percentile(interval_p95, 0.95)),
            "reprojection_pct_mean": rounded(statistics.fmean(reprojection)) if reprojection else 0.0,
            "reprojection_pct_max": rounded(max(reprojection)) if reprojection else 0.0,
            "dropped_window_max": max((int(sample.get("dropped") or 0) for sample in samples), default=0),
            "mispresented_window_max": max(
                (int(sample.get("mispresented") or 0) for sample in samples), default=0
            ),
            "system_cpu_pct_mean": rounded(statistics.fmean(system_cpu)) if system_cpu else 0.0,
            "system_cpu_pct_p95": rounded(cls._percentile(system_cpu, 0.95)),
            "system_gpu_pct_mean": (
                rounded(statistics.fmean(system_gpu)) if system_gpu else None
            ),
            "system_gpu_pct_p95": rounded(cls._percentile(system_gpu, 0.95)) if system_gpu else None,
        }

    @classmethod
    def _sample(cls, snapshot: object) -> dict[str, float | int | None]:
        return {
            "population": int(cls._value(snapshot, "vrc_population", 0)),
            "gpu_p50_ms": float(cls._value(snapshot, "gpu_p50_ms", 0.0)),
            "gpu_p95_ms": float(cls._value(snapshot, "gpu_p95_ms", 0.0)),
            "cpu_p95_ms": float(cls._value(snapshot, "cpu_p95_ms", 0.0)),
            "frame_interval_p95_ms": float(
                cls._value(snapshot, "frame_interval_p95_ms", 0.0)
            ),
            "reprojection_pct": float(cls._value(snapshot, "reprojection_pct", 0.0)),
            "dropped": int(cls._value(snapshot, "dropped", 0)),
            "mispresented": int(cls._value(snapshot, "mispresented", 0)),
            "system_cpu_pct": float(cls._value(snapshot, "system_cpu_pct", 0.0)),
            "system_gpu_pct": (
                None
                if cls._value(snapshot, "system_gpu_pct") is None
                else float(cls._value(snapshot, "system_gpu_pct", 0.0))
            ),
        }

    @classmethod
    def _settings(cls, snapshot: object) -> dict[str, object]:
        return {
            "refresh_hz": round(float(cls._value(snapshot, "refresh_hz", 0.0)), 3),
            "target_divisor": int(cls._value(snapshot, "target_divisor", 0)),
            "target_fps": round(float(cls._value(snapshot, "target_fps", 0.0)), 3),
            "budget_ms": round(float(cls._value(snapshot, "budget_ms", 0.0)), 4),
            "render_width": int(cls._value(snapshot, "render_width", 0)),
            "render_height": int(cls._value(snapshot, "render_height", 0)),
            "resolution_scale": int(cls._value(snapshot, "resolution_scale", 0)),
        }

    @classmethod
    def _settings_signature(cls, snapshot: object) -> tuple[object, ...]:
        settings = cls._settings(snapshot)
        return tuple(settings.values())

    @classmethod
    def _hardware(cls, hardware: object) -> dict[str, object]:
        return {
            "gpu_name": str(cls._value(hardware, "gpu_name", "")),
            "gpu_vram_mib": max(
                0, int(cls._value(hardware, "gpu_vram_mib", 0))
            ),
            "gpu_count": max(0, int(cls._value(hardware, "gpu_count", 0))),
            "cpu_name": str(cls._value(hardware, "cpu_name", "")),
            "cpu_physical_cores": max(
                0, int(cls._value(hardware, "cpu_physical_cores", 0))
            ),
            "cpu_logical_cores": max(
                0, int(cls._value(hardware, "cpu_logical_cores", 0))
            ),
            "system_ram_mib": max(
                0, int(cls._value(hardware, "system_ram_mib", 0))
            ),
            "hmd_manufacturer": str(cls._value(hardware, "hmd_manufacturer", "")),
            "hmd_model": str(cls._value(hardware, "hmd_model", "")),
        }

    def _base_record(
        self,
        record_type: str,
        snapshot: object,
        hardware: object,
    ) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "record_id": uuid.uuid4().hex,
            "record_type": record_type,
            "anonymous_contributor_id": self.contributor_id,
            "session_id": self.session_id,
            "world_id": str(self._value(snapshot, "vrc_world_id", "")),
            "population_bucket": str(self._value(snapshot, "vrc_population_bucket", "")),
            "settings": self._settings(snapshot),
            "hardware": self._hardware(hardware),
        }

    def _append_record(self, record: dict[str, object]) -> None:
        with self._file_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.records_path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                output.flush()
        world_id = str(record.get("world_id", ""))
        bucket = str(record.get("population_bucket", ""))
        if world_id:
            self._worlds.add(world_id)
        if world_id and bucket:
            self._contexts.add(f"{world_id}|{bucket}")
        if record.get("record_type") == "steady_window":
            self._steady_records += 1
        elif record.get("record_type") == "population_transition":
            self._transition_records += 1

    def _reset_steady(self) -> None:
        self._steady_key = None
        self._steady_started_at = 0.0
        self._steady_samples.clear()

    def _reset_world(self, world_id: str, population: int, now: float) -> None:
        self._current_world = world_id
        self._world_seen_at = now
        self._last_population = population
        self._last_settings_signature = None
        self._recent_samples.clear()
        self._transition = None
        self._reset_steady()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self._current_world = ""
            self._last_population = None
            self._last_settings_signature = None
            self._recent_samples.clear()
            self._transition = None
            self._reset_steady()

    def observe(self, snapshot: object, hardware: object, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic() if now is None else float(now)
        world_id = str(self._value(snapshot, "vrc_world_id", ""))
        bucket = str(self._value(snapshot, "vrc_population_bucket", ""))
        population = int(self._value(snapshot, "vrc_population", 0))
        ready = bool(self._value(snapshot, "vrc_context_ready", False))
        if (
            str(self._value(snapshot, "app_key", "")).casefold() != "steam.app.438100"
            or not ready
            or not world_id
            or not bucket
            or population <= 0
        ):
            return False
        if world_id != self._current_world:
            self._reset_world(world_id, population, now)
        if now - self._last_observed_at < self.sample_interval_seconds:
            return False
        self._last_observed_at = now

        sample = self._sample(snapshot)
        settings_signature = self._settings_signature(snapshot)
        if (
            self._last_settings_signature is not None
            and settings_signature != self._last_settings_signature
        ):
            self._recent_samples.clear()
            self._transition = None
            self._reset_steady()
        self._last_settings_signature = settings_signature
        previous_population = self._last_population
        population_delta = 0 if previous_population is None else population - previous_population
        emitted = False

        if population_delta:
            pre_cutoff = now - self.transition_pre_seconds
            pre_samples = [
                previous_sample
                for sampled_at, previous_sample in self._recent_samples
                if sampled_at >= pre_cutoff
            ]
            if (
                self._transition is None
                or self._transition.get("settings_signature") != settings_signature
            ):
                self._transition = {
                    "started_at": now,
                    "settings_signature": settings_signature,
                    "before_population": int(previous_population or 0),
                    "joins": max(0, population_delta),
                    "leaves": max(0, -population_delta),
                    "pre_samples": pre_samples,
                    "post_samples": [],
                    "snapshot": snapshot,
                    "hardware": hardware,
                }
            else:
                self._transition["joins"] = int(self._transition.get("joins", 0)) + max(
                    0, population_delta
                )
                self._transition["leaves"] = int(self._transition.get("leaves", 0)) + max(
                    0, -population_delta
                )
            self._reset_steady()
        self._last_population = population
        self._recent_samples.append((now, sample))

        if self._transition is not None:
            if self._transition.get("settings_signature") != settings_signature:
                self._transition = None
            else:
                post_samples = self._transition["post_samples"]
                assert isinstance(post_samples, list)
                post_samples.append(sample)
                if now - float(self._transition["started_at"]) >= self.transition_post_seconds:
                    pre_samples = self._transition["pre_samples"]
                    assert isinstance(pre_samples, list)
                    minimum_pre = max(3, int(self.transition_pre_seconds * 0.40))
                    minimum_post = max(5, int(self.transition_post_seconds * 0.65))
                    if len(pre_samples) >= minimum_pre and len(post_samples) >= minimum_post:
                        original_snapshot = self._transition["snapshot"]
                        record = self._base_record(
                            "population_transition",
                            original_snapshot,
                            self._transition["hardware"],
                        )
                        record.update(
                            {
                                "before_population": int(
                                    self._transition["before_population"]
                                ),
                                "after_population": population,
                                "joins": int(self._transition["joins"]),
                                "leaves": int(self._transition["leaves"]),
                                "duration_seconds": round(
                                    now - float(self._transition["started_at"]), 3
                                ),
                                "pre": self._summary(pre_samples),
                                "post": self._summary(post_samples),
                            }
                        )
                        self._append_record(record)
                        emitted = True
                    self._transition = None

        quiet_seconds = float(
            self._value(snapshot, "vrc_seconds_since_population_change", 0.0)
        )
        steady_key = (world_id, bucket, settings_signature)
        steady_eligible = (
            now - self._world_seen_at >= self.world_warmup_seconds
            and quiet_seconds >= self.steady_quiet_seconds
            and population_delta == 0
        )
        if not steady_eligible:
            self._reset_steady()
        else:
            if steady_key != self._steady_key:
                self._steady_key = steady_key
                self._steady_started_at = now
                self._steady_samples = []
            self._steady_samples.append(sample)
            if now - self._steady_started_at >= self.steady_window_seconds:
                minimum_samples = max(5, int(self.steady_window_seconds * 0.75))
                if len(self._steady_samples) >= minimum_samples:
                    record = self._base_record("steady_window", snapshot, hardware)
                    record.update(
                        {
                            "duration_seconds": round(
                                now - self._steady_started_at, 3
                            ),
                            "seconds_since_population_change": round(quiet_seconds, 3),
                            "metrics": self._summary(self._steady_samples),
                        }
                    )
                    self._append_record(record)
                    emitted = True
                self._reset_steady()
        return emitted

    def status(self) -> dict[str, object]:
        try:
            storage_bytes = self.records_path.stat().st_size
        except OSError:
            storage_bytes = 0
        return {
            "enabled": self.enabled,
            "worlds": len(self._worlds),
            "contexts": len(self._contexts),
            "steady_records": self._steady_records,
            "transition_records": self._transition_records,
            "records": self._steady_records + self._transition_records,
            "storage_bytes": storage_bytes,
            "records_path": str(self.records_path),
        }

    def export_share_package(self, destination: Path) -> Path:
        if not self.records_path.exists() or self.status()["records"] == 0:
            raise RuntimeError("尚无可导出的有效采集记录")
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "framepilot-vr-passive-telemetry",
            "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "summary": {
                key: value
                for key, value in self.status().items()
                if key not in {"records_path", "enabled"}
            },
            "privacy": {
                "contains_player_identifiers": False,
                "contains_instance_identifiers": False,
                "contains_machine_name": False,
                "contains_hardware_fingerprint": False,
                "world_id_included": True,
                "upload_performed": False,
            },
        }
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self._file_lock:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                archive.write(self.records_path, "records.jsonl")
        temporary.replace(destination)
        return destination

    def _upload_offsets(self) -> dict[str, int]:
        try:
            data = json.loads(self.upload_state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        offsets = data.get("offsets") if isinstance(data, dict) else None
        if not isinstance(offsets, dict):
            return {}
        output: dict[str, int] = {}
        for endpoint, value in offsets.items():
            try:
                output[str(endpoint)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return output

    def _save_upload_offset(
        self,
        endpoint: str,
        offset: int,
        batch_id: str,
    ) -> None:
        offsets = self._upload_offsets()
        offsets[endpoint] = max(0, int(offset))
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "offsets": offsets,
            "last_endpoint": endpoint,
            "last_batch_id": batch_id,
            "last_uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.upload_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.upload_state_path)

    def _pending_upload_batch(
        self,
        endpoint: str,
        *,
        max_records: int = 2_000,
        max_uncompressed_bytes: int = 7 * 1024 * 1024,
    ) -> tuple[list[bytes], int] | None:
        offsets = self._upload_offsets()
        offset = offsets.get(endpoint, 0)
        with self._file_lock:
            try:
                file_size = self.records_path.stat().st_size
            except OSError:
                return None
            if offset > file_size:
                offset = 0
            lines: list[bytes] = []
            total_bytes = 0
            end_offset = offset
            with self.records_path.open("rb") as stream:
                stream.seek(offset)
                while len(lines) < max_records:
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    if lines and total_bytes + len(line) > max_uncompressed_bytes:
                        break
                    try:
                        record = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise RuntimeError("本地共享记录包含无效 JSON，上传已停止")
                    if not isinstance(record, dict) or int(
                        record.get("schema_version", 0)
                    ) != self.SCHEMA_VERSION:
                        raise RuntimeError("本地共享记录版本不受支持，上传已停止")
                    lines.append(line)
                    total_bytes += len(line)
                    end_offset = stream.tell()
        return (lines, end_offset) if lines else None

    def _upload_archive(
        self,
        endpoint: str,
        lines: list[bytes],
    ) -> dict[str, object]:
        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "kind": "framepilot-vr-passive-telemetry",
            "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "summary": {"records": len(lines)},
            "privacy": {
                "contains_player_identifiers": False,
                "contains_instance_identifiers": False,
                "contains_machine_name": False,
                "contains_hardware_fingerprint": False,
                "world_id_included": True,
                "upload_performed": True,
            },
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="upload-", dir=self.root) as temporary:
            archive_path = Path(temporary) / "batch.zip"
            with zipfile.ZipFile(
                archive_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                )
                archive.writestr("records.jsonl", b"".join(lines))
            payload = archive_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/telemetry/batches",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/zip",
                "Content-Length": str(len(payload)),
                "X-Batch-SHA256": digest,
                "User-Agent": "FramePilotVR/0.7.4",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45.0) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = 0
            try:
                retry_after = int(exc.headers.get("Retry-After", "0"))
            except (AttributeError, TypeError, ValueError):
                pass
            try:
                error_payload = json.loads(detail)
                if isinstance(error_payload, dict):
                    retry_after = max(
                        retry_after,
                        int(error_payload.get("retry_after_seconds", 0)),
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            raise TelemetryUploadError(
                f"服务器拒绝上传（HTTP {exc.code}）：{detail}",
                http_status=int(exc.code),
                retry_after_seconds=retry_after,
            ) from exc
        except urllib.error.URLError as exc:
            raise TelemetryUploadError(f"无法连接共享服务器：{exc.reason}") from exc
        try:
            result = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("共享服务器返回了无效响应") from exc
        if not isinstance(result, dict) or not bool(result.get("ok", False)):
            raise RuntimeError("共享服务器未确认上传成功")
        return result

    def upload_pending(
        self,
        endpoint: str,
        *,
        max_batches: int = 20,
    ) -> dict[str, object]:
        endpoint = endpoint.rstrip("/")
        total_accepted = 0
        total_duplicates = 0
        uploaded_batches = 0
        last_batch_id = ""
        for _index in range(max(1, max_batches)):
            pending = self._pending_upload_batch(endpoint)
            if pending is None:
                break
            lines, end_offset = pending
            result = self._upload_archive(endpoint, lines)
            last_batch_id = str(result.get("batch_id", ""))
            self._save_upload_offset(endpoint, end_offset, last_batch_id)
            uploaded_batches += 1
            total_accepted += int(result.get("accepted_records", 0))
            total_duplicates += int(result.get("duplicate_records", 0))
        has_more = self._pending_upload_batch(endpoint) is not None
        return {
            "ok": True,
            "batches": uploaded_batches,
            "accepted_records": total_accepted,
            "duplicate_records": total_duplicates,
            "last_batch_id": last_batch_id,
            "has_more": has_more,
        }
