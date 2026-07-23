from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import time


WORLD_PATTERN = re.compile(r"\bJoining\s+(wrld_[0-9a-fA-F-]+)(?::|\s|$)")
PLAYER_JOIN_PATTERN = re.compile(r"\bOnPlayerJoined\s+(.+?)\s*$")
PLAYER_LEFT_PATTERN = re.compile(r"\bOnPlayerLeft\s+(.+?)\s*$")
INITIAL_POPULATION_QUIET_SECONDS = 1.5
LOG_DISCOVERY_INTERVAL_SECONDS = 1.0
PROFILE_SAVE_INTERVAL_SECONDS = 5.0
SAFE_STREAK_SAMPLES = 8
PRESSURE_STREAK_SAMPLES = 3


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

    def _process_line(self, line: str, now: float) -> None:
        world_match = WORLD_PATTERN.search(line)
        if world_match:
            self.world_id = world_match.group(1)
            self.player_hashes.clear()
            self.joined_at = now
            self.last_population_change_at = now
            self.join_times.clear()
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
        )


class VrcResolutionProfileStore:
    """Learns proven-safe and proven-unsafe scales for local VRC contexts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, object] = {"schema_version": 1, "profiles": {}}
        self.safe_streaks: dict[str, tuple[int, int]] = {}
        self.pressure_streaks: dict[str, tuple[int, int]] = {}
        self.last_save_at = -1e9
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("schema_version") == 1:
                self.data = loaded
        except (OSError, ValueError, TypeError):
            pass

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
            and mispresented == 0
        )
        pressure = (
            gpu_ms >= budget_ms * 0.92
            or reprojection_pct >= 3.0
            or dropped > 0
            or mispresented > 0
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
