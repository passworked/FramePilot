from __future__ import annotations

import unittest
from unittest.mock import patch

from steamvr_core import AdaptiveRuntime, FrameSample, RuntimeConfig, SteamVRSession


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


if __name__ == "__main__":
    unittest.main()
