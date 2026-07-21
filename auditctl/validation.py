from __future__ import annotations

import json
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any

VALID_REF_PREFIXES = ("wi:", "ka:", "ad:", "sha:", "pr:", "sprint:", "capsule:")
EVENT_ID_RE = re.compile(r"^ad:[0-9A-HJKMNP-TV-Z]{26}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_FIELDS = {
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


def validate_timestamp(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("timestamp must be an ISO UTC value ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    return value


def validate_event_id(value: str) -> str:
    if not EVENT_ID_RE.match(value):
        raise ValueError(f"invalid audit event id: {value}")
    return value


def validate_refs(refs: list[str]) -> list[str]:
    for ref in refs:
        if not ref.startswith(VALID_REF_PREFIXES):
            prefixes = ", ".join(VALID_REF_PREFIXES)
            raise ValueError(f"invalid ref '{ref}'; expected one of: {prefixes}")
    return refs


def parse_metadata(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata must be a JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_payload_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def observation_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Return the audit-specific body carried by the shared observation envelope."""
    return {
        "summary": event["summary"],
        "detail": event.get("detail"),
        "refs": event["refs"],
        "source": event["source"],
        "metadata": event["metadata"],
    }


def with_observation_envelope(
    event: dict[str, Any],
    *,
    origin_stream_id: str,
    origin_seq: int,
) -> dict[str, Any]:
    """Add the producer-outbox envelope without replacing auditctl's stable ID."""
    metadata = event["metadata"]
    payload = observation_payload(event)
    return {
        **event,
        "event_id": event["id"],
        "schema_version": 1,
        "record_class": "observation",
        "origin_stream_id": origin_stream_id,
        "origin_seq": origin_seq,
        "event_type": event["type"],
        "runtime_session_id": metadata.get("runtime_session_id"),
        "occurred_at": event["ts"],
        "basis_revision": metadata.get("basis_revision"),
        "correlation_id": metadata.get("correlation_id"),
        "causation_id": metadata.get("causation_id"),
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            canonical_payload_json(payload).encode("utf-8")
        ).hexdigest(),
    }


def validate_event_object(event: dict[str, Any]) -> dict[str, Any]:
    required = {"id", "ts", "type", "actor", "summary", "refs", "source", "metadata", "created_at"}
    missing = sorted(required - set(event))
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    validate_event_id(str(event["id"]))
    validate_timestamp(str(event["ts"]))
    validate_timestamp(str(event["created_at"]))
    if not isinstance(event["refs"], list) or not all(isinstance(r, str) for r in event["refs"]):
        raise ValueError("refs must be a list of strings")
    validate_refs(event["refs"])
    if not isinstance(event["metadata"], dict):
        raise ValueError("metadata must be an object")
    for key in ("type", "actor", "summary", "source"):
        if not isinstance(event[key], str) or not event[key]:
            raise ValueError(f"{key} must be a non-empty string")
    if event.get("detail") is not None and not isinstance(event["detail"], str):
        raise ValueError("detail must be a string or null")
    present_envelope_fields = ENVELOPE_FIELDS & set(event)
    if present_envelope_fields:
        missing_envelope_fields = sorted(ENVELOPE_FIELDS - set(event))
        if missing_envelope_fields:
            raise ValueError(
                f"incomplete observation envelope; missing: {', '.join(missing_envelope_fields)}"
            )
        if event["event_id"] != event["id"]:
            raise ValueError("event_id must match the stable audit id")
        if event["event_type"] != event["type"]:
            raise ValueError("event_type must match the audit event type")
        if event["occurred_at"] != event["ts"]:
            raise ValueError("occurred_at must match the audit timestamp")
        if event["schema_version"] != 1:
            raise ValueError("schema_version must be 1")
        if event["record_class"] != "observation":
            raise ValueError("record_class must be observation")
        try:
            uuid.UUID(str(event["origin_stream_id"]))
        except (ValueError, AttributeError) as exc:
            raise ValueError("origin_stream_id must be a UUID") from exc
        if (
            isinstance(event["origin_seq"], bool)
            or not isinstance(event["origin_seq"], int)
            or event["origin_seq"] < 1
        ):
            raise ValueError("origin_seq must be a positive integer")
        for key in ("runtime_session_id", "basis_revision", "correlation_id", "causation_id"):
            if event[key] is not None and not isinstance(event[key], str):
                raise ValueError(f"{key} must be a string or null")
        if not isinstance(event["payload_sha256"], str) or not SHA256_RE.match(event["payload_sha256"]):
            raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
        expected_payload = observation_payload(event)
        if event["payload"] != expected_payload:
            raise ValueError("payload does not match the canonical audit payload mapping")
        expected_digest = hashlib.sha256(
            canonical_payload_json(expected_payload).encode("utf-8")
        ).hexdigest()
        if event["payload_sha256"] != expected_digest:
            raise ValueError("payload_sha256 does not match the canonical audit payload")
    return event
