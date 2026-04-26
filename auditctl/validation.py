from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

VALID_REF_PREFIXES = ("wi:", "ka:", "ad:", "sha:", "pr:", "sprint:")
EVENT_ID_RE = re.compile(r"^ad:[0-9A-HJKMNP-TV-Z]{26}$")


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
    return event

