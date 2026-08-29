from __future__ import annotations

import json
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any

VALID_REF_PREFIXES = ("wi:", "ka:", "ad:", "sha:", "pr:", "sprint:", "capsule:", "baseline:")
EVENT_ID_RE = re.compile(r"^ad:[0-9A-HJKMNP-TV-Z]{26}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_RESULT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMMUTABLE_RESULT_REF_RE = re.compile(r"^artifact:sha256:[0-9a-f]{64}$")
ACTIONQ_SESSION_EXIT_BOUNDS = {
    "phase": 32,
    "terminal_status": 64,
    "terminal_reason": 256,
    "dispatch_result_ref": 128,
    "dispatch_result_digest": 71,
}
ACTIONQ_TERMINAL_REASON_CODES = frozenset(
    {
        "completed",
        "process-exit",
        "start-failed",
        "cancelled",
        "timeout",
        "usage-limit",
        "crash-inferred",
    }
)
ACTIONQ_SESSION_ID_MAX_BYTES = 128

#: The resolver's own account of the write, attached by `auditctl add` and by nothing else.
#:
#: Deliberately NOT part of ENVELOPE_FIELDS. That set is validated all-or-nothing -- if any
#: member is present every member must be -- so adding a field to it would invalidate every
#: event written before today, including the 1593 already on disk. This one is optional by
#: construction: absent on historical events, complete when present.
RESOLVED_CONTEXT_FIELDS = (
    "repo_id",
    "repo_root",
    "artifacts_root",
    "published_from",
    "resolution_source",
)
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


def validate_actionq_session_exit_metadata(event: dict[str, Any]) -> None:
    """Validate the activated, bounded ActionQ session-exit result projection."""
    if event.get("type") != "session.exit" or event.get("source") != "actionq-daemon":
        return

    metadata = event["metadata"]
    result_keys = ("dispatch_result_ref", "dispatch_result_digest")
    # Older session.exit publishers and shards may use any metadata shape,
    # including nulls under names introduced by this additive contract.  A
    # non-null result field is the explicit activation boundary.
    if not any(metadata.get(key) is not None for key in result_keys):
        return

    required = {
        "action_id",
        "session_id",
        "runtime_session_id",
        "phase",
        "terminal_status",
        "terminal_reason",
        *result_keys,
    }
    missing = sorted(key for key in required if metadata.get(key) is None)
    if missing:
        raise ValueError(
            "activated dispatch-result metadata is missing: " + ", ".join(missing)
        )

    action_id = metadata["action_id"]
    if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id < 1:
        raise ValueError("action_id must be a positive integer when dispatch-result metadata is active")

    session_id = metadata["session_id"]
    runtime_session_id = metadata["runtime_session_id"]
    for key, value in (("session_id", session_id), ("runtime_session_id", runtime_session_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string when dispatch-result metadata is active")
        if len(value.encode("utf-8")) > ACTIONQ_SESSION_ID_MAX_BYTES:
            raise ValueError(f"{key} exceeds the {ACTIONQ_SESSION_ID_MAX_BYTES}-byte bound")
    if session_id != runtime_session_id:
        raise ValueError("session_id and runtime_session_id must match when dispatch-result metadata is active")
    if "event_id" in event and event.get("runtime_session_id") != runtime_session_id:
        raise ValueError(
            "metadata.runtime_session_id must match the observation-envelope runtime_session_id"
        )
    expected_actor = f"actionq:{runtime_session_id}"
    if event.get("actor") != expected_actor:
        raise ValueError(f"actor must equal {expected_actor!r} for activated dispatch-result metadata")

    for key, limit in ACTIONQ_SESSION_EXIT_BOUNDS.items():
        value = metadata[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"{key} exceeds the {limit}-byte bound")

    terminal_reason = metadata["terminal_reason"]
    if terminal_reason not in ACTIONQ_TERMINAL_REASON_CODES:
        allowed = ", ".join(sorted(ACTIONQ_TERMINAL_REASON_CODES))
        raise ValueError(f"terminal_reason must be a recognized safe reason code: {allowed}")

    result_ref = metadata["dispatch_result_ref"]
    result_digest = metadata["dispatch_result_digest"]
    if not IMMUTABLE_RESULT_REF_RE.fullmatch(result_ref):
        raise ValueError(
            "dispatch_result_ref must be an artifact:sha256:<64 lowercase hex> reference"
        )
    if not IMMUTABLE_RESULT_DIGEST_RE.fullmatch(result_digest):
        raise ValueError(
            "dispatch_result_digest must be a sha256:<64 lowercase hex> digest"
        )
    if result_ref.removeprefix("artifact:") != result_digest:
        raise ValueError("dispatch result reference and digest must identify the same result")


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


def validate_resolved_context(event: dict[str, Any]) -> None:
    """Optional when absent, complete when present.

    Partial context is worse than none: a reader who finds `published_from` missing from
    half the events cannot tell "this write did not record it" from "this write came from
    the repository itself", and that ambiguity is the whole defect this field exists to
    remove. So the field is either entirely absent or entirely there.
    """
    if "resolved_context" not in event:
        return
    context = event["resolved_context"]
    if not isinstance(context, dict):
        raise ValueError("resolved_context must be an object")
    missing = sorted(set(RESOLVED_CONTEXT_FIELDS) - set(context))
    if missing:
        raise ValueError(f"incomplete resolved_context; missing: {', '.join(missing)}")
    unknown = sorted(set(context) - set(RESOLVED_CONTEXT_FIELDS))
    if unknown:
        raise ValueError(f"unknown resolved_context field(s): {', '.join(unknown)}")
    for key in RESOLVED_CONTEXT_FIELDS:
        if not isinstance(context[key], str) or not context[key]:
            raise ValueError(f"resolved_context.{key} must be a non-empty string")


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
    validate_actionq_session_exit_metadata(event)
    for key in ("type", "actor", "summary", "source"):
        if not isinstance(event[key], str) or not event[key]:
            raise ValueError(f"{key} must be a non-empty string")
    if event.get("detail") is not None and not isinstance(event["detail"], str):
        raise ValueError("detail must be a string or null")
    validate_resolved_context(event)
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
