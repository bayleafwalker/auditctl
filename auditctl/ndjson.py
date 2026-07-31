from __future__ import annotations

import fcntl
import glob
import json
import os
from pathlib import Path
from typing import Iterator

from .validation import canonical_json, validate_event_object


# Keep individual audit records small enough that the append-only recovery
# shards remain practical to reconcile.  Large immutable payloads belong in an
# artifact referenced by the event, not in the ledger line itself.
MAX_EVENT_LINE_BYTES = 16 * 1024


class ImportInputError(ValueError):
    """A safe, stable classification for rejected rebuild input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _validation_code(raw: object) -> str:
    if not isinstance(raw, dict):
        return "malformed_record"
    envelope_fields = {
        "event_id",
        "schema_version",
        "record_class",
        "origin_stream_id",
        "origin_seq",
        "event_type",
        "runtime_session_id",
        "occurred_at",
        "basis_revision",
        "correlation_id",
        "causation_id",
        "payload",
        "payload_sha256",
    }
    if envelope_fields & set(raw):
        if envelope_fields - set(raw):
            return "malformed_envelope"
        if raw.get("schema_version") != 1:
            return "unsupported_schema"
        if raw.get("record_class") != "observation":
            return "unsupported_record_class"
        return "malformed_envelope"
    return "malformed_record"


def append_event(path: Path, event: dict) -> None:
    line = (canonical_json(event) + "\n").encode("utf-8")
    if len(line) > MAX_EVENT_LINE_BYTES:
        raise ValueError(
            f"audit event exceeds the {MAX_EVENT_LINE_BYTES}-byte canonical NDJSON limit; "
            "store bulk payloads as an immutableRef kind=artifact under "
            "_artifacts/<repo_id>/ instead"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        offset = 0
        while offset < len(line):
            written = os.write(fd, line[offset:])
            if written <= 0:
                raise OSError("short write while appending audit event")
            offset += written
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def resolve_inputs(raw_path: str) -> list[Path]:
    path = Path(raw_path).expanduser()
    if any(char in raw_path for char in "*?["):
        return [Path(p) for p in sorted(glob.glob(raw_path))]
    if path.is_dir():
        return sorted(path.glob("events-*.ndjson"))
    return [path]


def read_events(paths: list[Path]) -> Iterator[dict]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise ImportInputError("corrupt_shard") from exc
                    try:
                        yield validate_event_object(raw)
                    except (TypeError, ValueError) as exc:
                        raise ImportInputError(_validation_code(raw)) from exc
        except FileNotFoundError as exc:
            raise ImportInputError("missing_shard") from exc
        except OSError as exc:
            raise ImportInputError("unreadable_shard") from exc
