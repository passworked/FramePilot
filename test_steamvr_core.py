from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from framepilot_i18n import LocalizedMessage
from steamvr_core import (
    AdaptiveController,
    AdaptiveRuntime,
    FrameSample,
    HardwareContext,
    RuntimeConfig,
    SteamVRSession,
    WindowStats,
)
from vrc_context import (
    VrcContextSnapshot,
    VrcProfileEstimate,
    VrcResolutionProfileStore,
)


class _GpuSampler:
    def sample(self, now: float) -> float:
        return 35.0


class _SessionBase:
    def __init__(self) -> None:
        self.frame_index = 0
        self.scale = 100
        self.scale_writes: list[int] = []

    def scene_application(self) -> tuple[int, str]:
        return 1234, "steam.app.test"

    def frame_batch(self, count: int = 128) -> list[FrameSample]:
        frames = []
        for _ in range(36):
            self.frame_index += 1
            frames.append(
                FrameSample(
                    index=self.frame_index,
                    timestamp=self.frame_index / 72.0,
                    gpu_ms=40.0,
                    cpu_ms=4.0,
                    interval_ms=1000.0 / 72.0,
                    dropped=0,
                    mispresented=0,
                    reprojection=False,
                )
            )
        return frames

    def get_scale(self, app_key: str) -> tuple[int, bool]:
        return self.scale, True

    def set_scale(self, app_key: str, scale: int) -> None:
        self.scale = scale
        self.scale_writes.append(scale)

    def refresh_rate(self) -> float:
        return 72.0

    def recommended_size(self) -> tuple[int, int]:
        return 2000, 2000


class _DashboardSession(_SessionBase):
    def __init__(self, visible: bool | None) -> None:
        super().__init__()
        self.visible = visible

    def dashboard_visible(self) -> bool | None:
        return self.visible


class _VrcSession(_SessionBase):
    def __init__(self) -> None:
        super().__init__()
        self.scale = 130

    def scene_application(self) -> tuple[int, str]:
        return 1234, "steam.app.438100"


class _VrcProvider:
    def poll(self, now: float) -> VrcContextSnapshot:
        return VrcContextSnapshot(
            world_id="wrld_test",
            population=8,
            population_bucket="6-10",
            ready=True,
            joined_at=1.0,
            last_population_change_at=1.0,
            seconds_since_population_change=max(0.0, now - 1.0),
        )


class DashboardVisibilityTests(unittest.TestCase):
    @staticmethod
    def runtime(session: _SessionBase) -> AdaptiveRuntime:
        runtime = AdaptiveRuntime(
            RuntimeConfig(
                mode="continuous",
                armed=True,
                target_divisor=2,
                window_seconds=1.0,
                evaluate_seconds=0.1,
                cooldown_seconds=0.0,
            )
        )
        runtime.session = session  # type: ignore[assignment]
        runtime.gpu_sampler = _GpuSampler()  # type: ignore[assignment]
        runtime.connected = True
        return runtime

    def poll(self, runtime: AdaptiveRuntime, now: float):
        with patch("steamvr_core.process_running", return_value=True):
            return runtime.poll(now)

    def test_dashboard_visible_freezes_downshift_but_keeps_metrics(self) -> None:
        session = _DashboardSession(True)
        snapshot = self.poll(self.runtime(session), 1.0)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.decision.action, "hold")
        self.assertIn("Dashboard/桌面面板可见", snapshot.adaptive_frozen_reason)
        self.assertEqual(snapshot.application_fps, 72.0)
        self.assertEqual(session.scale_writes, [])

    def test_dashboard_hide_waits_for_fresh_stable_window(self) -> None:
        session = _DashboardSession(True)
        runtime = self.runtime(session)
        self.poll(runtime, 1.0)
        session.visible = False

        for now in (1.5, 2.0, 2.5):
            snapshot = self.poll(runtime, now)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.decision.action, "hold")
            self.assertNotEqual(snapshot.adaptive_frozen_reason, "")
            self.assertEqual(session.scale_writes, [])

        recovered = self.poll(runtime, 3.0)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.adaptive_frozen_reason, "")
        self.assertTrue(recovered.write_applied)
        self.assertEqual(session.scale_writes, [99])

    def test_dashboard_read_failure_uses_fail_safe_freeze(self) -> None:
        session = _DashboardSession(None)
        snapshot = self.poll(self.runtime(session), 1.0)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.decision.action, "hold")
        self.assertIn("无法可靠读取", snapshot.adaptive_frozen_reason)
        self.assertEqual(session.scale_writes, [])

    def test_legacy_simulated_provider_without_reader_remains_compatible(self) -> None:
        session = _SessionBase()
        snapshot = self.poll(self.runtime(session), 1.0)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertTrue(snapshot.write_applied)
        self.assertEqual(session.scale_writes, [99])

    def test_openvr_reader_reports_visible_and_read_errors(self) -> None:
        class Overlay:
            def __init__(self, result: bool | Exception) -> None:
                self.result = result

            def isDashboardVisible(self) -> bool:
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        session = SteamVRSession()
        session.overlay = Overlay(True)
        self.assertIs(session.dashboard_visible(), True)
        session.overlay = Overlay(RuntimeError("OpenVR read failed"))
        self.assertIsNone(session.dashboard_visible())


class StructuredLocalizationEventTests(unittest.TestCase):
    def test_target_frame_rate_change_uses_semantic_event_key(self) -> None:
        runtime = AdaptiveRuntime(
            RuntimeConfig(
                mode="continuous",
                armed=True,
                target_divisor=1,
            )
        )
        runtime.hardware_context = HardwareContext(
            "test",
            "machine",
            "gpu",
            "maker",
            "hmd",
            72.0,
            2000,
            2000,
        )

        runtime.update_config(
            replace(runtime.config, target_divisor=2)
        )

        events = runtime.drain_events()
        self.assertEqual(len(events), 1)
        level, message = events[0]
        self.assertEqual(level, "info")
        self.assertIsInstance(message, LocalizedMessage)
        assert isinstance(message, LocalizedMessage)
        self.assertEqual(message.key, "event.target_fps_lowered")
        self.assertEqual(message.values["old_fps"], 72.0)
        self.assertEqual(message.values["new_fps"], 36.0)


class RecoveryPolicyTests(unittest.TestCase):
    def test_quest_style_mispresented_does_not_block_recovery(self) -> None:
        controller = AdaptiveController()
        config = RuntimeConfig(
            min_scale=20,
            max_scale=150,
            step_up=5,
            raise_stable_seconds=12.0,
            target_divisor=4,
        )
        stats = WindowStats(
            frames=68,
            gpu_p50_ms=20.0,
            gpu_p95_ms=28.0,
            cpu_p95_ms=16.0,
            interval_p95_ms=30.0,
            reprojection_pct=0.0,
            dropped=0,
            mispresented=190,
        )

        observing = controller.decide(stats, 1000.0 / 22.5, 71, 0.0, config)
        recovered = controller.decide(stats, 1000.0 / 22.5, 71, 12.1, config)

        self.assertEqual(observing.action, "hold")
        self.assertEqual(recovered.action, "up")
        self.assertEqual(recovered.proposed_scale, 76)

    def test_predicted_recovery_is_capped_by_step_up(self) -> None:
        controller = AdaptiveController()
        config = RuntimeConfig(
            min_scale=20,
            max_scale=200,
            step_up=5,
            raise_stable_seconds=0.0,
            target_divisor=4,
        )
        stats = WindowStats(68, 10.0, 12.0, 8.0, 30.0, 0.0, 0, 0)

        decision = controller.decide(stats, 1000.0 / 22.5, 20, 1.0, config)

        self.assertEqual(decision.action, "up")
        self.assertEqual(decision.proposed_scale, 25)

    def test_v1_profile_quarantines_unsafe_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="framepilot-profile-test-") as temporary:
            path = Path(temporary) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "hardware": {
                                "world|1-5|d4": {
                                    "safe_scale": 80,
                                    "safe_evidence": 12,
                                    "unsafe_scale": 20,
                                    "pressure_samples": 400,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = VrcResolutionProfileStore(path)
            profile = store.get("hardware", "world", "1-5", "d4")

            self.assertEqual(store.data["schema_version"], 3)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile["safe_scale"], 80)
            self.assertEqual(profile["unsafe_scale"], 0)
            self.assertEqual(profile["pressure_samples"], 0)
            self.assertEqual(profile["legacy_v1_unsafe_scale"], 20)
            self.assertEqual(profile["legacy_v1_pressure_samples"], 400)

    def test_local_experience_direct_raise_respects_jump_limit(self) -> None:
        runtime = AdaptiveRuntime(
            RuntimeConfig(
                mode="continuous",
                armed=True,
                vrc_experience_delay_seconds=5.0,
                vrc_experience_max_raise=15,
            )
        )
        runtime.current_scale = 100
        runtime.vrc_context = VrcContextSnapshot(
            world_id="wrld_test",
            population=12,
            population_bucket="11-20",
            ready=True,
        )
        runtime.vrc_context_stable_since = 0.0
        estimate = VrcProfileEstimate(
            target_scale=140,
            basis_scale=140,
            source_bucket="11-20",
            kind="exact",
            confidence=0.8,
            samples=60,
            unsafe_scale=0,
            population_adjustment=0,
        )
        stats = WindowStats(60, 5.0, 6.0, 5.0, 12.0, 0.0, 0, 0)

        decision = runtime._vrc_experience_decision(
            estimate,
            "experience-key",
            stats,
            11.111,
            50.0,
            5.1,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.action, "up")
        self.assertEqual(decision.proposed_scale, 115)

    def test_local_experience_can_directly_lower_scale(self) -> None:
        runtime = AdaptiveRuntime(
            RuntimeConfig(
                mode="continuous",
                armed=True,
                vrc_experience_delay_seconds=0.0,
            )
        )
        runtime.current_scale = 130
        runtime.vrc_context = VrcContextSnapshot(
            world_id="wrld_test",
            population=24,
            population_bucket="21-40",
            ready=True,
        )
        estimate = VrcProfileEstimate(
            target_scale=105,
            basis_scale=110,
            source_bucket="11-20",
            kind="extrapolated",
            confidence=0.7,
            samples=60,
            unsafe_scale=0,
            population_adjustment=-5,
        )
        overloaded = WindowStats(60, 20.0, 25.0, 20.0, 25.0, 10.0, 3, 0)

        decision = runtime._vrc_experience_decision(
            estimate,
            "experience-key",
            overloaded,
            11.111,
            99.0,
            1.0,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.action, "down")
        self.assertEqual(decision.proposed_scale, 105)

    def test_auto_mode_applies_saved_world_scale_but_suggest_mode_does_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="framepilot-runtime-experience-"
        ) as temporary:
            profile_path = Path(temporary) / "profiles.json"

            def make_runtime(mode: str) -> tuple[AdaptiveRuntime, _VrcSession]:
                runtime = AdaptiveRuntime(
                    RuntimeConfig(
                        mode="continuous",
                        armed=True,
                        target_divisor=2,
                        window_seconds=1.0,
                        evaluate_seconds=0.1,
                        cooldown_seconds=0.0,
                        vrc_experience_mode=mode,
                        vrc_experience_delay_seconds=0.0,
                    )
                )
                session = _VrcSession()
                runtime.session = session  # type: ignore[assignment]
                runtime.gpu_sampler = _GpuSampler()  # type: ignore[assignment]
                runtime.vrc_context_provider = _VrcProvider()  # type: ignore[assignment]
                runtime.vrc_profile_store = VrcResolutionProfileStore(
                    profile_path
                )
                runtime.vrc_profile_store.data["profiles"] = {
                    "hardware": {
                        "wrld_test|6-10|d2": {
                            "world_id": "wrld_test",
                            "population_bucket": "6-10",
                            "target": "d2",
                            "safe_scale": 100,
                            "safe_evidence": 30,
                            "unsafe_scale": 0,
                            "samples": 60,
                            "updated_at": "",
                        }
                    }
                }
                runtime.hardware_context = HardwareContext(
                    "hardware",
                    "machine",
                    "gpu",
                    "maker",
                    "hmd",
                    72.0,
                    2000,
                    2000,
                )
                runtime.connected = True
                return runtime, session

            automatic, automatic_session = make_runtime("auto")
            suggestions, suggestion_session = make_runtime("suggest")
            with patch("steamvr_core.process_running", return_value=True):
                automatic_snapshot = automatic.poll(10.0)
                suggestion_snapshot = suggestions.poll(10.0)

            self.assertIsNotNone(automatic_snapshot)
            self.assertIsNotNone(suggestion_snapshot)
            assert automatic_snapshot is not None
            assert suggestion_snapshot is not None
            self.assertEqual(automatic_session.scale_writes, [100])
            self.assertTrue(automatic_snapshot.write_applied)
            self.assertEqual(suggestion_session.scale_writes, [])
            self.assertFalse(suggestion_snapshot.write_applied)
            self.assertEqual(
                suggestion_snapshot.decision.proposed_scale,
                100,
            )


if __name__ == "__main__":
    unittest.main()
