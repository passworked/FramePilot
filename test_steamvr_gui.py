from __future__ import annotations

import unittest

from steamvr_adaptive_gui import (
    AUTO_UPLOAD_MIN_INTERVAL_SECONDS,
    auto_upload_due,
    clear_persisted_write_unlock,
)


class AutoUploadSchedulingTests(unittest.TestCase):
    def test_existing_backlog_uploads_as_one_due_operation(self) -> None:
        due, wake_at = auto_upload_due(151, 0, 1_000.0, 0.0, 0.0)

        self.assertTrue(due)
        self.assertEqual(wake_at, 1_000.0)

    def test_new_single_record_waits_for_batch_interval(self) -> None:
        due, wake_at = auto_upload_due(
            152,
            151,
            1_060.0,
            1_000.0,
            0.0,
        )

        self.assertFalse(due)
        self.assertEqual(
            wake_at,
            1_000.0 + AUTO_UPLOAD_MIN_INTERVAL_SECONDS,
        )

    def test_record_threshold_can_flush_after_normal_cooldown(self) -> None:
        due, _wake_at = auto_upload_due(
            176,
            151,
            2_000.0,
            1_000.0,
            1_500.0,
        )

        self.assertTrue(due)

    def test_force_does_not_bypass_server_retry_time(self) -> None:
        due, wake_at = auto_upload_due(
            176,
            151,
            2_000.0,
            1_000.0,
            3_000.0,
            force=True,
        )

        self.assertFalse(due)
        self.assertEqual(wake_at, 3_000.0)


class _FakeSettings:
    def __init__(self) -> None:
        self.values = {"runtime/armed": True}

    def remove(self, key: str) -> None:
        self.values.pop(key, None)


class WriteSafetyTests(unittest.TestCase):
    def test_saved_write_unlock_is_cleared_at_startup(self) -> None:
        settings = _FakeSettings()

        armed = clear_persisted_write_unlock(settings)  # type: ignore[arg-type]

        self.assertFalse(armed)
        self.assertNotIn("runtime/armed", settings.values)


if __name__ == "__main__":
    unittest.main()
