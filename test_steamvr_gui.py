from __future__ import annotations

import unittest

from steamvr_adaptive_gui import (
    AUTO_UPLOAD_MIN_INTERVAL_SECONDS,
    STEAMVR_LAUNCH_URI,
    auto_upload_due,
    clear_persisted_write_unlock,
    request_steamvr_start,
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


class SteamVRAutostartTests(unittest.TestCase):
    def test_running_steamvr_does_not_open_uri(self) -> None:
        opened: list[str] = []

        state, detail = request_steamvr_start(
            process_checker=lambda _name: True,
            url_opener=opened.append,
        )

        self.assertEqual((state, detail), ("already_running", ""))
        self.assertEqual(opened, [])

    def test_stopped_steamvr_opens_official_steam_uri(self) -> None:
        opened: list[str] = []

        state, detail = request_steamvr_start(
            process_checker=lambda _name: False,
            url_opener=opened.append,
        )

        self.assertEqual((state, detail), ("requested", ""))
        self.assertEqual(opened, [STEAMVR_LAUNCH_URI])

    def test_uri_launch_failure_is_reported(self) -> None:
        def fail_to_open(_uri: str) -> None:
            raise OSError("no Steam handler")

        state, detail = request_steamvr_start(
            process_checker=lambda _name: False,
            url_opener=fail_to_open,
        )

        self.assertEqual(state, "failed")
        self.assertEqual(detail, "no Steam handler")


if __name__ == "__main__":
    unittest.main()
