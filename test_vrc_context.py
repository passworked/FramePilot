from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from vrc_context import (
    PassiveVrcDataCollector,
    TelemetryUploadError,
    VrcResolutionProfileStore,
)


class LocalWorldExperienceTests(unittest.TestCase):
    @staticmethod
    def profile(
        world_id: str,
        bucket: str,
        scale: int,
        evidence: int = 30,
        unsafe: int = 0,
    ) -> dict[str, object]:
        return {
            "world_id": world_id,
            "population_bucket": bucket,
            "target": "d2",
            "safe_scale": scale,
            "safe_evidence": evidence,
            "unsafe_scale": unsafe,
            "samples": 60,
            "updated_at": "",
        }

    def store(self, root: str) -> VrcResolutionProfileStore:
        store = VrcResolutionProfileStore(Path(root) / "profiles.json")
        store.data["profiles"] = {
            "hardware": {
                "wrld_test|6-10|d2": self.profile(
                    "wrld_test", "6-10", 125
                ),
                "wrld_test|21-40|d2": self.profile(
                    "wrld_test", "21-40", 100
                ),
            }
        }
        return store

    def test_exact_population_bucket_uses_saved_scale(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="framepilot-experience-test-"
        ) as temporary:
            store = self.store(temporary)

            estimate = store.estimate(
                "hardware",
                "wrld_test",
                8,
                "6-10",
                "d2",
            )

            self.assertIsNotNone(estimate)
            assert estimate is not None
            self.assertEqual(estimate.kind, "exact")
            self.assertEqual(estimate.target_scale, 125)

    def test_missing_middle_bucket_interpolates_local_profiles(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="framepilot-experience-test-"
        ) as temporary:
            store = self.store(temporary)

            estimate = store.estimate(
                "hardware",
                "wrld_test",
                15,
                "11-20",
                "d2",
            )

            self.assertIsNotNone(estimate)
            assert estimate is not None
            self.assertEqual(estimate.kind, "interpolated")
            self.assertEqual(estimate.source_bucket, "6-10↔21-40")
            self.assertEqual(estimate.target_scale, 115)

    def test_heavier_population_uses_balanced_tier_offset(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="framepilot-experience-test-"
        ) as temporary:
            store = self.store(temporary)
            hardware = store.data["profiles"]["hardware"]  # type: ignore[index]
            assert isinstance(hardware, dict)
            hardware.pop("wrld_test|21-40|d2")

            estimate = store.estimate(
                "hardware",
                "wrld_test",
                24,
                "21-40",
                "d2",
                compensation="balanced",
            )

            self.assertIsNotNone(estimate)
            assert estimate is not None
            self.assertEqual(estimate.kind, "extrapolated")
            self.assertEqual(estimate.population_adjustment, -10)
            self.assertEqual(estimate.target_scale, 115)

    def test_forget_current_world_preserves_other_worlds(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="framepilot-experience-test-"
        ) as temporary:
            store = self.store(temporary)
            hardware = store.data["profiles"]["hardware"]  # type: ignore[index]
            assert isinstance(hardware, dict)
            hardware["wrld_other|6-10|d2"] = self.profile(
                "wrld_other", "6-10", 110
            )

            removed = store.forget("hardware", "wrld_test")

            self.assertEqual(removed, 2)
            self.assertEqual(len(hardware), 1)
            self.assertIn("wrld_other|6-10|d2", hardware)


class PassiveUploadTests(unittest.TestCase):
    def test_backlog_is_combined_into_one_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="framepilot-upload-test-") as temporary:
            collector = PassiveVrcDataCollector(Path(temporary))
            collector.root.mkdir(parents=True, exist_ok=True)
            collector.records_path.write_text(
                "".join(
                    f'{{"schema_version":1,"record_id":"{index:032x}"}}\n'
                    for index in range(151)
                ),
                encoding="utf-8",
            )
            calls: list[int] = []

            def fake_upload(_endpoint: str, lines: list[bytes]) -> dict[str, object]:
                calls.append(len(lines))
                return {
                    "ok": True,
                    "batch_id": "batch-1",
                    "accepted_records": len(lines),
                    "duplicate_records": 0,
                }

            collector._upload_archive = fake_upload  # type: ignore[method-assign]
            result = collector.upload_pending("https://example.invalid")

            self.assertEqual(calls, [151])
            self.assertEqual(result["batches"], 1)
            self.assertEqual(result["accepted_records"], 151)
            self.assertFalse(result["has_more"])

    def test_upload_error_retains_rate_limit_metadata(self) -> None:
        error = TelemetryUploadError(
            "rate limited",
            http_status=429,
            retry_after_seconds=1_234,
        )

        self.assertEqual(error.http_status, 429)
        self.assertEqual(error.retry_after_seconds, 1_234)


if __name__ == "__main__":
    unittest.main()
