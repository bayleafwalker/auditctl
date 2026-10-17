"""Read-only source snapshot qualification for a later S4 import review.

This module verifies explicit frozen files and Auditctl-owned event semantics. It
does not discover shards, open the configured database, write an import, or
establish an S4 authority/cutover decision.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .central import prepare_observation
from .validation import validate_event_object


_HEX = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_SCHEMA = "auditctl-source-snapshot-manifest/v1"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 512 * 1024 * 1024


class SourcePreflightError(ValueError):
    """A frozen source set cannot be qualified under this contract."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourcePreflightError("duplicate JSON object key")
        result[key] = value
    return result


def _json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(
                SourcePreflightError("non-standard JSON number")
            ),
            parse_float=lambda value: _finite_float(value),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, SourcePreflightError):
            raise
        raise SourcePreflightError(f"invalid {label} JSON") from error


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise SourcePreflightError("non-finite JSON number")
    return number


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != fields:
        raise SourcePreflightError(f"invalid {label} fields")
    return value


def _nonempty(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise SourcePreflightError(f"{label} must be a non-empty string")
    return value


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise SourcePreflightError(f"{label} must be a positive integer")
    return value


def _load_manifest(raw: bytes, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise SourcePreflightError("manifest exceeds the 4 MiB bound")
    if type(expected_sha256) is not str or not _HEX.fullmatch(expected_sha256):
        raise SourcePreflightError("expected manifest SHA-256 must be lowercase hex")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise SourcePreflightError("manifest differs from its frozen SHA-256")
    manifest = _exact_fields(
        _json(raw, label="manifest"), {"schema_version", "sources"}, "manifest"
    )
    if manifest["schema_version"] != _MANIFEST_SCHEMA:
        raise SourcePreflightError("unsupported source manifest schema")
    if type(manifest["sources"]) is not list or not manifest["sources"]:
        raise SourcePreflightError("manifest sources must be a non-empty array")
    seen: set[str] = set()
    total_bytes = 0
    for source in manifest["sources"]:
        _exact_fields(
            source,
            {"source_id", "source_repo_id", "scope", "path", "byte_count", "line_count", "sha256"},
            "source descriptor",
        )
        source_id = _nonempty(source["source_id"], "source_id")
        if source_id in seen:
            raise SourcePreflightError("duplicate source_id in manifest")
        seen.add(source_id)
        _nonempty(source["source_repo_id"], "source_repo_id")
        if type(source["scope"]) is not str or source["scope"] not in {"committed-shard", "reviewed-tail"}:
            raise SourcePreflightError("unsupported source scope")
        _nonempty(source["path"], "source path")
        total_bytes += _positive(source["byte_count"], "source byte_count")
        _positive(source["line_count"], "source line_count")
        if type(source["sha256"]) is not str or not _HEX.fullmatch(source["sha256"]):
            raise SourcePreflightError("source sha256 must be lowercase hex")
    if total_bytes > _MAX_SOURCE_BYTES:
        raise SourcePreflightError("source set exceeds the 512 MiB bound")
    return manifest, actual


def _read_frozen_source(source: dict[str, Any], manifest_path: Path) -> bytes:
    path = Path(source["path"])
    if not path.is_absolute():
        path = manifest_path.parent / path
    try:
        if path.is_symlink() or not path.is_file():
            raise SourcePreflightError("source path must name an explicit regular file")
        with path.open("rb") as stream:
            raw = stream.read(source["byte_count"] + 1)
    except OSError as error:
        raise SourcePreflightError("frozen source file is unavailable") from error
    if len(raw) != source["byte_count"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise SourcePreflightError(f"frozen source changed: {source['source_id']}")
    if not raw.endswith(b"\n"):
        raise SourcePreflightError(f"frozen source lacks final newline: {source['source_id']}")
    lines = raw.splitlines(keepends=True)
    if len(lines) != source["line_count"] or any(not line.strip() for line in lines):
        raise SourcePreflightError(f"frozen source line framing changed: {source['source_id']}")
    return raw


def preflight(manifest_path: Path, expected_manifest_sha256: str) -> dict[str, Any]:
    """Verify explicitly bound source bytes and return a redacted comparison report."""
    try:
        with manifest_path.open("rb") as stream:
            manifest_raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise SourcePreflightError("source manifest is unavailable") from error
    manifest, manifest_sha256 = _load_manifest(manifest_raw, expected_manifest_sha256)

    events_by_id: dict[str, dict[str, Any]] = {}
    producer_tuples: dict[tuple[str, int], tuple[str, str]] = {}
    idempotency_keys: dict[str, tuple[str, str]] = {}
    locations: list[dict[str, Any]] = []
    duplicate_copies = 0
    total_lines = 0
    for source in manifest["sources"]:
        source_raw = _read_frozen_source(source, manifest_path)
        byte_offset = 0
        for line_number, raw_line in enumerate(source_raw.splitlines(keepends=True), start=1):
            event = _json(raw_line.rstrip(b"\r\n"), label="event")
            if type(event) is not dict:
                raise SourcePreflightError("source record must be a JSON object")
            try:
                validate_event_object(event)
                prepared = prepare_observation(event) if "origin_stream_id" in event else None
            except (ValueError, KeyError) as error:
                raise SourcePreflightError(
                    f"invalid Auditctl event at {source['source_id']}:{line_number}: {error}"
                ) from error
            event_id = _nonempty(event.get("id"), "event id")
            raw_hash = hashlib.sha256(raw_line).hexdigest()
            prior = events_by_id.get(event_id)
            if prior is not None:
                if prior["raw_line_sha256"] != raw_hash or prior["source_repo_id"] != source["source_repo_id"]:
                    raise SourcePreflightError(f"conflicting stable event identity: {event_id}")
                duplicate_copies += 1
            else:
                events_by_id[event_id] = {
                    "raw_line_sha256": raw_hash,
                    "source_repo_id": source["source_repo_id"],
                }

            if prepared is not None:
                producer_tuple = (prepared.origin_stream_id, prepared.origin_seq)
                tuple_prior = producer_tuples.get(producer_tuple)
                if tuple_prior is not None and tuple_prior != (event_id, raw_hash):
                    raise SourcePreflightError("producer origin tuple collision")
                producer_tuples[producer_tuple] = (event_id, raw_hash)
                if "record_sha256" in event:
                    supplied_record_digest = event["record_sha256"]
                    if type(supplied_record_digest) is not str or not _HEX.fullmatch(supplied_record_digest):
                        raise SourcePreflightError("record_sha256 has an unsupported shape")
                    if supplied_record_digest != prepared.record_sha256:
                        raise SourcePreflightError("record_sha256 differs from Auditctl central-v1 digest")
                    source_record = {"status": "present", "domain": "auditctl-central-v1", "sha256": supplied_record_digest}
                else:
                    source_record = {"status": "absent", "domain": None, "sha256": None}
                derived_record_digest = prepared.record_sha256
                payload_digest = {"status": "present", "sha256": prepared.payload_sha256}
                record_class = prepared.record_class
            else:
                if "record_sha256" in event:
                    source_record = {"status": "unknown-domain", "domain": None, "sha256": None}
                else:
                    source_record = {"status": "absent", "domain": None, "sha256": None}
                derived_record_digest = None
                payload_digest = {"status": "absent", "sha256": None}
                record_class = None

            key = event.get("idempotency_key")
            if key is not None:
                key = _nonempty(key, "idempotency_key")
                key_prior = idempotency_keys.get(key)
                if key_prior is not None and key_prior != (event_id, raw_hash):
                    raise SourcePreflightError("idempotency key collision")
                idempotency_keys[key] = (event_id, raw_hash)
            import_fingerprint = hashlib.sha256(
                (
                    "auditctl-s4-import-fingerprint/v1\0"
                    f"{manifest_sha256}\0{source['source_repo_id']}\0{event_id}\0{raw_hash}"
                ).encode("utf-8")
            ).hexdigest()
            locations.append({
                "source_id": source["source_id"],
                "line": line_number,
                "byte_offset": byte_offset,
                "raw_line_sha256": raw_hash,
                "event_id": event_id,
                "origin_stream_id": prepared.origin_stream_id if prepared is not None else None,
                "origin_seq": prepared.origin_seq if prepared is not None else None,
                "record_class": record_class,
                "source_payload_sha256": payload_digest,
                "source_record_sha256": source_record,
                "derived_auditctl_central_v1_record_sha256": derived_record_digest,
                "import_fingerprint_sha256": import_fingerprint,
            })
            byte_offset += len(raw_line)
            total_lines += 1

    sequences: dict[str, set[int]] = {}
    for item in locations:
        if item["origin_stream_id"] is not None:
            sequences.setdefault(item["origin_stream_id"], set()).add(item["origin_seq"])
    gaps: list[dict[str, Any]] = []
    for stream_id, values in sorted(sequences.items()):
        ordered = sorted(values)
        if ordered and ordered[0] != 1:
            gaps.append({"origin_stream_id": stream_id, "after": 0, "before": ordered[0]})
        for before, after in zip(ordered, ordered[1:]):
            if after != before + 1:
                gaps.append({"origin_stream_id": stream_id, "after": before, "before": after})
    if gaps:
        raise SourcePreflightError("origin sequence gap in frozen source set")

    return {
        "schema_version": "auditctl-s4-source-preflight/v1",
        "qualification": "source-only-qualified",
        "s4_authority_established": False,
        "cutover_authorized": False,
        "manifest_sha256": manifest_sha256,
        "source_count": len(manifest["sources"]),
        "source_bindings": [
            {key: source[key] for key in ("source_id", "source_repo_id", "scope", "byte_count", "line_count", "sha256")}
            for source in manifest["sources"]
        ],
        "line_count": total_lines,
        "distinct_event_count": len(events_by_id),
        "exact_duplicate_copies": duplicate_copies,
        "records": locations,
        "producer_stream_count": len(sequences),
        "idempotency_key_count": len(idempotency_keys),
    }
