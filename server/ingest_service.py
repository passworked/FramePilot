from __future__ import annotations

import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from typing import BinaryIO
import zipfile


SCHEMA_VERSION = 1
MAX_UPLOAD_BYTES = int(os.environ.get("FRAMEPILOT_MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
MAX_UNCOMPRESSED_BYTES = int(
    os.environ.get("FRAMEPILOT_MAX_UNCOMPRESSED_BYTES", 50 * 1024 * 1024)
)
MAX_RECORDS = int(os.environ.get("FRAMEPILOT_MAX_RECORDS", 20_000))
DATA_ROOT = Path(os.environ.get("FRAMEPILOT_DATA_ROOT", "/srv/framepilot"))
DB_PATH = DATA_ROOT / "db" / "framepilot.sqlite3"
OBJECT_ROOT = DATA_ROOT / "objects" / "sha256"
TEMP_ROOT = DATA_ROOT / "tmp"
ORIGIN_SECRET_FILE = Path(
    os.environ.get("FRAMEPILOT_ORIGIN_SECRET_FILE", "/etc/framepilot/origin-secret")
)
LISTEN_HOST = os.environ.get("FRAMEPILOT_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FRAMEPILOT_LISTEN_PORT", "8080"))
WORLD_PATTERN = re.compile(r"^wrld_[0-9a-fA-F-]{16,}$")
HEX_ID_PATTERN = re.compile(r"^[0-9a-f]{32,64}$")
BLOCKED_KEYS = {
    "hardware_id",
    "instance_id",
    "instance_url",
    "local_time",
    "machine_name",
    "player_id",
    "player_name",
    "user_id",
    "username",
}
BATCH_STORE_LOCK = threading.Lock()


class ValidationError(ValueError):
    pass


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def initialize_storage() -> None:
    for path in (DB_PATH.parent, OBJECT_ROOT, TEMP_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    with closing(database()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                received_at INTEGER NOT NULL,
                object_key TEXT NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                submitted_records INTEGER NOT NULL,
                accepted_records INTEGER NOT NULL,
                duplicate_records INTEGER NOT NULL,
                contributor_id TEXT NOT NULL,
                source_ip_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                record_type TEXT NOT NULL,
                contributor_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                world_id TEXT NOT NULL,
                population_bucket TEXT NOT NULL,
                refresh_hz REAL NOT NULL,
                target_fps REAL NOT NULL,
                resolution_scale INTEGER NOT NULL,
                population_min INTEGER NOT NULL,
                population_max INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                received_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS records_world_population
                ON records(world_id, population_bucket);
            CREATE INDEX IF NOT EXISTS records_contributor
                ON records(contributor_id);
            CREATE INDEX IF NOT EXISTS records_received
                ON records(received_at);

            CREATE TABLE IF NOT EXISTS hourly_limits (
                source_ip_hash TEXT NOT NULL,
                hour_bucket INTEGER NOT NULL,
                requests INTEGER NOT NULL,
                uploaded_bytes INTEGER NOT NULL,
                PRIMARY KEY(source_ip_hash, hour_bucket)
            );
            """
        )
        connection.commit()


def origin_secret() -> str:
    try:
        value = ORIGIN_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Cannot read origin secret: {exc}") from exc
    if len(value) < 32:
        raise RuntimeError("Origin secret must contain at least 32 characters")
    return value


def recursive_blocked_key(value: object) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if normalized in BLOCKED_KEYS:
                return normalized
            found = recursive_blocked_key(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = recursive_blocked_key(nested)
            if found:
                return found
    return None


def validated_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValidationError("Every JSONL line must be an object")
    if int(record.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValidationError("Unsupported record schema_version")
    blocked = recursive_blocked_key(record)
    if blocked:
        raise ValidationError(f"Privacy-sensitive key is not accepted: {blocked}")
    record_id = str(record.get("record_id", "")).casefold()
    contributor_id = str(record.get("anonymous_contributor_id", "")).casefold()
    session_id = str(record.get("session_id", "")).casefold()
    if not HEX_ID_PATTERN.fullmatch(record_id):
        raise ValidationError("Invalid record_id")
    if not HEX_ID_PATTERN.fullmatch(contributor_id):
        raise ValidationError("Invalid anonymous_contributor_id")
    if not HEX_ID_PATTERN.fullmatch(session_id):
        raise ValidationError("Invalid session_id")
    world_id = str(record.get("world_id", ""))
    if not WORLD_PATTERN.fullmatch(world_id):
        raise ValidationError("Invalid world_id")
    record_type = str(record.get("record_type", ""))
    if record_type not in {"steady_window", "population_transition"}:
        raise ValidationError("Invalid record_type")
    population_bucket = str(record.get("population_bucket", ""))
    if population_bucket not in {"1-5", "6-10", "11-20", "21-40", "41+"}:
        raise ValidationError("Invalid population_bucket")
    settings = record.get("settings")
    if not isinstance(settings, dict):
        raise ValidationError("Missing settings")
    hardware = record.get("hardware")
    if not isinstance(hardware, dict):
        raise ValidationError("Missing hardware")
    for key in ("gpu_name", "hmd_manufacturer", "hmd_model", "cpu_name"):
        if key in hardware and (
            not isinstance(hardware[key], str) or len(hardware[key]) > 256
        ):
            raise ValidationError(f"Invalid hardware.{key}")
    hardware_ranges = {
        "gpu_vram_mib": (0, 1024 * 1024),
        "gpu_count": (0, 64),
        "cpu_physical_cores": (0, 1024),
        "cpu_logical_cores": (0, 2048),
        "system_ram_mib": (0, 16 * 1024 * 1024),
    }
    for key, (minimum, maximum) in hardware_ranges.items():
        if key not in hardware:
            continue
        value = hardware[key]
        if isinstance(value, bool):
            raise ValidationError(f"Invalid hardware.{key}")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Invalid hardware.{key}") from exc
        if number != value or not minimum <= number <= maximum:
            raise ValidationError(f"Invalid hardware.{key}")
    metrics_key = "metrics" if record_type == "steady_window" else "post"
    metrics = record.get(metrics_key)
    if not isinstance(metrics, dict):
        raise ValidationError(f"Missing {metrics_key} metrics")
    return record


def validate_archive(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError("Body is not a valid ZIP archive") from exc
    with archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if names != {"manifest.json", "records.jsonl"} or len(entries) != 2:
            raise ValidationError("Archive must contain only manifest.json and records.jsonl")
        if any(entry.flag_bits & 0x1 for entry in entries):
            raise ValidationError("Encrypted ZIP entries are not accepted")
        if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
            raise ValidationError("Uncompressed archive is too large")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Invalid manifest.json") from exc
        if not isinstance(manifest, dict):
            raise ValidationError("manifest.json must be an object")
        if int(manifest.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValidationError("Unsupported manifest schema_version")
        if manifest.get("kind") != "framepilot-vr-passive-telemetry":
            raise ValidationError("Invalid manifest kind")
        records: list[dict[str, object]] = []
        record_ids: set[str] = set()
        try:
            stream = archive.open("records.jsonl")
            for line_number, raw_line in enumerate(stream, start=1):
                if line_number > MAX_RECORDS:
                    raise ValidationError("Archive contains too many records")
                if len(raw_line) > 1024 * 1024:
                    raise ValidationError(f"JSONL line {line_number} is too large")
                if not raw_line.strip():
                    continue
                try:
                    candidate = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(f"Invalid JSONL line {line_number}") from exc
                record = validated_record(candidate)
                record_id = str(record["record_id"])
                if record_id in record_ids:
                    raise ValidationError(f"Duplicate record_id in line {line_number}")
                record_ids.add(record_id)
                records.append(record)
        except KeyError as exc:
            raise ValidationError("records.jsonl is missing") from exc
        if not records:
            raise ValidationError("Archive contains no records")
        return manifest, records


def hash_source_ip(value: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()


def rate_limit(connection: sqlite3.Connection, ip_hash: str, upload_bytes: int) -> None:
    hour_bucket = int(time.time() // 3600)
    connection.execute(
        """
        INSERT INTO hourly_limits(source_ip_hash, hour_bucket, requests, uploaded_bytes)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(source_ip_hash, hour_bucket) DO UPDATE SET
            requests = requests + 1,
            uploaded_bytes = uploaded_bytes + excluded.uploaded_bytes
        """,
        (ip_hash, hour_bucket, upload_bytes),
    )
    row = connection.execute(
        """
        SELECT requests, uploaded_bytes
        FROM hourly_limits
        WHERE source_ip_hash=? AND hour_bucket=?
        """,
        (ip_hash, hour_bucket),
    ).fetchone()
    assert row is not None
    if int(row["requests"]) > 30 or int(row["uploaded_bytes"]) > 100 * 1024 * 1024:
        raise PermissionError("Hourly upload limit exceeded")
    connection.execute("DELETE FROM hourly_limits WHERE hour_bucket < ?", (hour_bucket - 48,))


def record_index_values(
    record: dict[str, object], batch_id: str, received_at: int
) -> tuple[object, ...]:
    settings = record["settings"]
    assert isinstance(settings, dict)
    metrics_key = "metrics" if record["record_type"] == "steady_window" else "post"
    metrics = record[metrics_key]
    assert isinstance(metrics, dict)
    return (
        str(record["record_id"]),
        batch_id,
        str(record["record_type"]),
        str(record["anonymous_contributor_id"]),
        str(record["session_id"]),
        str(record["world_id"]),
        str(record["population_bucket"]),
        float(settings.get("refresh_hz", 0.0)),
        float(settings.get("target_fps", 0.0)),
        int(settings.get("resolution_scale", 0)),
        int(metrics.get("population_min", 0)),
        int(metrics.get("population_max", 0)),
        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        received_at,
    )


def store_batch(
    temporary_path: Path,
    sha256: str,
    compressed_bytes: int,
    records: list[dict[str, object]],
    source_ip: str,
    secret: str,
) -> dict[str, object]:
    with BATCH_STORE_LOCK:
        return _store_batch_locked(
            temporary_path,
            sha256,
            compressed_bytes,
            records,
            source_ip,
            secret,
        )


def _store_batch_locked(
    temporary_path: Path,
    sha256: str,
    compressed_bytes: int,
    records: list[dict[str, object]],
    source_ip: str,
    secret: str,
) -> dict[str, object]:
    batch_id = f"batch_{sha256[:32]}"
    object_key = f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}.zip"
    object_path = DATA_ROOT / "objects" / object_key
    contributor_id = str(records[0]["anonymous_contributor_id"])
    if any(str(record["anonymous_contributor_id"]) != contributor_id for record in records):
        raise ValidationError("A batch may contain only one anonymous contributor")
    ip_hash = hash_source_ip(source_ip, secret)
    received_at = int(time.time())
    with closing(database()) as connection:
        with connection:
            existing = connection.execute(
                "SELECT accepted_records, duplicate_records FROM batches WHERE sha256=?",
                (sha256,),
            ).fetchone()
            if existing is not None:
                temporary_path.unlink(missing_ok=True)
                return {
                    "ok": True,
                    "batch_id": batch_id,
                    "accepted_records": int(existing["accepted_records"]),
                    "duplicate_records": int(existing["duplicate_records"]),
                    "duplicate": True,
                }
            rate_limit(connection, ip_hash, compressed_bytes)
            object_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, object_path)
            connection.execute(
                """
                INSERT INTO batches(
                    batch_id, sha256, received_at, object_key, compressed_bytes,
                    submitted_records, accepted_records, duplicate_records,
                    contributor_id, source_ip_hash
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    batch_id,
                    sha256,
                    received_at,
                    object_key,
                    compressed_bytes,
                    len(records),
                    contributor_id,
                    ip_hash,
                ),
            )
            accepted = 0
            for record in records:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO records(
                        record_id, batch_id, record_type, contributor_id, session_id,
                        world_id, population_bucket, refresh_hz, target_fps,
                        resolution_scale, population_min, population_max,
                        payload_json, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    record_index_values(record, batch_id, received_at),
                )
                accepted += max(0, cursor.rowcount)
            duplicates = len(records) - accepted
            connection.execute(
                """
                UPDATE batches
                SET accepted_records=?, duplicate_records=?
                WHERE batch_id=?
                """,
                (accepted, duplicates, batch_id),
            )
    return {
        "ok": True,
        "batch_id": batch_id,
        "accepted_records": accepted,
        "duplicate_records": duplicates,
        "duplicate": accepted == 0,
    }


def stream_upload(stream: BinaryIO, content_length: int) -> tuple[Path, str]:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    remaining = content_length
    descriptor, name = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=TEMP_ROOT)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ValidationError("Request body ended early")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        return path, digest.hexdigest()
    except Exception:
        path.unlink(missing_ok=True)
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "FramePilotIngest/1"

    def log_message(self, format_string: str, *args: object) -> None:
        message = format_string % args
        print(
            json.dumps(
                {
                    "time": int(time.time()),
                    "remote": self.client_address[0],
                    "message": message,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            with closing(database()) as connection:
                batches = int(
                    connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
                )
                records = int(
                    connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                )
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "framepilot-ingest",
                    "schema_version": SCHEMA_VERSION,
                    "batches": batches,
                    "records": records,
                },
            )
        except Exception:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "storage_unavailable"},
            )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/telemetry/batches":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        temporary_path: Path | None = None
        try:
            secret = origin_secret()
            supplied_secret = self.headers.get("X-FramePilot-Origin-Secret", "")
            if not hmac.compare_digest(supplied_secret, secret):
                self.send_json(
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": "origin_auth_failed"},
                )
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type not in {"application/zip", "application/octet-stream"}:
                raise ValidationError("Content-Type must be application/zip")
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
                self.send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"ok": False, "error": "invalid_upload_size"},
                )
                return
            temporary_path, actual_sha256 = stream_upload(self.rfile, content_length)
            supplied_sha256 = self.headers.get("X-Batch-SHA256", "").casefold()
            if supplied_sha256 and not hmac.compare_digest(
                supplied_sha256, actual_sha256
            ):
                raise ValidationError("X-Batch-SHA256 does not match request body")
            _manifest, records = validate_archive(temporary_path)
            source_ip = self.headers.get(
                "CF-Connecting-IP", self.client_address[0]
            ).strip()
            result = store_batch(
                temporary_path,
                actual_sha256,
                content_length,
                records,
                source_ip,
                secret,
            )
            temporary_path = None
            self.send_json(HTTPStatus.OK, result)
        except PermissionError as exc:
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"ok": False, "error": "rate_limited", "detail": str(exc)},
            )
        except (ValidationError, ValueError) as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_batch", "detail": str(exc)},
            )
        except Exception:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "internal_error"},
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def main() -> None:
    initialize_storage()
    origin_secret()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "event": "listening",
                "host": LISTEN_HOST,
                "port": LISTEN_PORT,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "nonce": secrets.token_hex(4),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
