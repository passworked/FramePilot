from __future__ import annotations

import math
import unittest

from steamvr_core import FrameSample, application_framerate
from steamvr_overlay import overlay_rows


class ApplicationFramerateTests(unittest.TestCase):
    @staticmethod
    def samples(fps: float, count: int = 10) -> list[FrameSample]:
        return [
            FrameSample(
                index=index,
                timestamp=index / fps,
                gpu_ms=4.0,
                cpu_ms=2.0,
                interval_ms=4.08,
                dropped=0,
                mispresented=0,
                reprojection=False,
            )
            for index in range(count)
        ]

    def test_uses_compositor_timestamps_instead_of_client_interval(self) -> None:
        result = application_framerate(self.samples(36.0), refresh_hz=72.0)

        self.assertTrue(math.isclose(result, 36.0))

    def test_sparse_poll_samples_use_compositor_frame_index_delta(self) -> None:
        samples = self.samples(36.0, count=4)
        sparse_samples = [samples[0], samples[3]]

        result = application_framerate(sparse_samples, refresh_hz=72.0)

        self.assertTrue(math.isclose(result, 36.0))

    def test_never_exceeds_headset_refresh(self) -> None:
        result = application_framerate(self.samples(245.0), refresh_hz=72.0)

        self.assertEqual(result, 72.0)

    def test_osd_shows_application_framerate_for_live_regression_case(self) -> None:
        data = {
            "refresh_hz": 72.0,
            "application_fps": 72.0,
            "target_fps": 72.0,
            "budget_ms": 1000.0 / 72.0,
            "frame_interval_p95_ms": 4.08,
        }

        label, value, _color = overlay_rows(data, ["fps"])[0]

        self.assertEqual(label, "APP FRAMERATE")
        self.assertEqual(value, "72.0 FPS")

    def test_old_packet_falls_back_to_honest_refresh_metric(self) -> None:
        data = {
            "refresh_hz": 72.0,
            "target_fps": 72.0,
            "budget_ms": 1000.0 / 72.0,
            "frame_interval_p95_ms": 4.08,
        }

        label, value, _color = overlay_rows(data, ["fps"])[0]

        self.assertEqual(label, "HMD REFRESH")
        self.assertEqual(value, "72 Hz")

    def test_osd_marks_dashboard_guard_as_frozen(self) -> None:
        data = {
            "resolution_scale": 81,
            "proposed_scale": 81,
            "decision": "hold",
            "adaptive_frozen_reason": "SteamVR Dashboard/桌面面板可见",
        }

        label, value, _color = overlay_rows(data, ["decision"])[0]

        self.assertEqual(label, "SCHEDULER")
        self.assertEqual(value, "FROZEN  81% > 81%")

    def test_osd_distinguishes_recovery_wait_from_generic_hold(self) -> None:
        data = {
            "resolution_scale": 71,
            "proposed_scale": 71,
            "decision": "hold",
            "reason": "性能余量观察中 7/12 秒",
            "observation_phase": "pre_up_stable",
        }

        _label, value, _color = overlay_rows(data, ["decision"])[0]

        self.assertEqual(value, "WAIT  71% > 71%")


if __name__ == "__main__":
    unittest.main()
