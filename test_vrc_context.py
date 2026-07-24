from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from vrc_context import PassiveVrcDataCollector, TelemetryUploadError


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
