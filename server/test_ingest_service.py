from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from server import ingest_service


WORLD_ID = "wrld_11111111-1111-1111-1111-111111111111"


def valid_record(record_id: str = "1" * 32) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_id": record_id,
        "record_type": "steady_window",
        "anonymous_contributor_id": "2" * 32,
        "session_id": "3" * 32,
        "world_id": WORLD_ID,
        "population_bucket": "11-20",
        "settings": {
            "refresh_hz": 90.0,
            "target_divisor": 2,
            "target_fps": 45.0,
            "budget_ms": 22.2222,
            "render_width": 2136,
            "render_height": 2136,
            "resolution_scale": 120,
        },
        "hardware": {
            "gpu_name": "Test GPU",
            "gpu_vram_mib": 12288,
            "gpu_count": 1,
            "cpu_name": "Test CPU",
            "cpu_physical_cores": 8,
            "cpu_logical_cores": 16,
            "system_ram_mib": 32768,
            "hmd_manufacturer": "Test",
            "hmd_model": "Test HMD",
        },
        "duration_seconds": 60.0,
        "seconds_since_population_change": 90.0,
        "metrics": {
            "sample_count": 61,
            "population_min": 14,
            "population_max": 14,
            "population_mean": 14.0,
        },
    }


def archive_bytes(records: list[dict[str, object]]) -> bytes:
    output = io.BytesIO()
    manifest = {
        "schema_version": 1,
        "kind": "framepilot-vr-passive-telemetry",
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(
            "records.jsonl",
            "".join(json.dumps(record) + "\n" for record in records),
        )
    return output.getvalue()


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.patches = (
            patch.object(ingest_service, "DATA_ROOT", root),
            patch.object(ingest_service, "DB_PATH", root / "db" / "framepilot.sqlite3"),
            patch.object(
                ingest_service, "OBJECT_ROOT", root / "objects" / "sha256"
            ),
            patch.object(ingest_service, "TEMP_ROOT", root / "tmp"),
        )
        for active_patch in self.patches:
            active_patch.start()
        ingest_service.initialize_storage()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    def write_archive(self, records: list[dict[str, object]]) -> tuple[Path, str]:
        payload = archive_bytes(records)
        path = ingest_service.TEMP_ROOT / "batch.zip"
        path.write_bytes(payload)
        return path, hashlib.sha256(payload).hexdigest()

    def test_valid_batch_is_indexed_and_idempotent(self) -> None:
        path, digest = self.write_archive([valid_record()])
        _manifest, records = ingest_service.validate_archive(path)
        first = ingest_service.store_batch(
            path, digest, path.stat().st_size, records, "203.0.113.10", "s" * 64
        )
        self.assertEqual(first["accepted_records"], 1)
        self.assertFalse(first["duplicate"])

        path, digest = self.write_archive([valid_record()])
        duplicate = ingest_service.store_batch(
            path, digest, path.stat().st_size, records, "203.0.113.10", "s" * 64
        )
        self.assertTrue(duplicate["duplicate"])
        with closing(ingest_service.database()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)

    def test_privacy_sensitive_field_is_rejected(self) -> None:
        record = valid_record()
        record["machine_name"] = "not-allowed"
        path, _digest = self.write_archive([record])
        with self.assertRaisesRegex(ingest_service.ValidationError, "machine_name"):
            ingest_service.validate_archive(path)

    def test_invalid_hardware_capacity_is_rejected(self) -> None:
        record = valid_record()
        record["hardware"]["system_ram_mib"] = -1  # type: ignore[index]
        path, _digest = self.write_archive([record])
        with self.assertRaisesRegex(
            ingest_service.ValidationError, "hardware.system_ram_mib"
        ):
            ingest_service.validate_archive(path)

    def test_hash_mapped_object_path_is_immutable(self) -> None:
        path, digest = self.write_archive([valid_record()])
        _manifest, records = ingest_service.validate_archive(path)
        ingest_service.store_batch(
            path, digest, path.stat().st_size, records, "203.0.113.10", "s" * 64
        )
        expected = (
            ingest_service.DATA_ROOT
            / "objects"
            / "sha256"
            / digest[:2]
            / digest[2:4]
            / f"{digest}.zip"
        )
        self.assertTrue(expected.exists())

    def test_rate_limit_reports_seconds_until_next_hour(self) -> None:
        with closing(ingest_service.database()) as connection:
            with patch.object(ingest_service.time, "time", return_value=3_601.0):
                for _index in range(30):
                    ingest_service.rate_limit(connection, "source", 1)
                with self.assertRaises(ingest_service.RateLimitError) as raised:
                    ingest_service.rate_limit(connection, "source", 1)

        self.assertEqual(raised.exception.retry_after_seconds, 3_599)


if __name__ == "__main__":
    unittest.main()
