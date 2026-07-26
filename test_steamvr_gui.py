from __future__ import annotations

import unittest

from steamvr_adaptive_gui import (
    AUTO_UPLOAD_MIN_INTERVAL_SECONDS,
    ONBOARDING_PAGE_BUILDERS,
    ONBOARDING_PAGE_COUNT,
    SHOW_AB_EXPERIMENT_UI,
    STEAMVR_LAUNCH_URI,
    auto_upload_due,
    cached_write_permission,
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
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values or {}

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


class WritePermissionCacheTests(unittest.TestCase):
    def test_saved_write_permission_is_restored_at_startup(self) -> None:
        settings = _FakeSettings({"runtime/armed": True})

        armed = cached_write_permission(settings)  # type: ignore[arg-type]

        self.assertTrue(armed)

    def test_missing_write_permission_defaults_to_locked(self) -> None:
        settings = _FakeSettings()

        armed = cached_write_permission(settings)  # type: ignore[arg-type]

        self.assertFalse(armed)


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


class ProductionUiTests(unittest.TestCase):
    def test_ab_experiment_controls_are_hidden(self) -> None:
        self.assertFalse(SHOW_AB_EXPERIMENT_UI)

    def test_onboarding_includes_language_as_its_own_first_step(self) -> None:
        self.assertEqual(ONBOARDING_PAGE_COUNT, 4)
        self.assertEqual(ONBOARDING_PAGE_BUILDERS[0], "_language_page")
        self.assertEqual(ONBOARDING_PAGE_BUILDERS[-1], "_welcome_page")


if __name__ == "__main__":
    unittest.main()
