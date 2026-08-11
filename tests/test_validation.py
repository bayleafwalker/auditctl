from __future__ import annotations

import pytest

from auditctl.validation import (
    parse_metadata,
    validate_event_object,
    validate_refs,
    validate_timestamp,
    with_observation_envelope,
)


def test_invalid_ref_errors() -> None:
    with pytest.raises(ValueError, match="invalid ref"):
        validate_refs(["bad:1"])


def test_capsule_ref_prefix_is_valid() -> None:
    assert validate_refs(["capsule:01f2b3c4-5555-4666-8777-999999999999"]) == [
        "capsule:01f2b3c4-5555-4666-8777-999999999999"
    ]


def test_invalid_timestamp_errors() -> None:
    with pytest.raises(ValueError, match="ending in Z"):
        validate_timestamp("2026-04-26T10:00:00+00:00")


def test_metadata_must_be_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_metadata("[]")


def test_actionq_session_exit_result_projection_is_bounded_and_consistent() -> None:
    event = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "session.exit",
        "actor": "actionq:session-1",
        "summary": "Session exit",
        "detail": None,
        "refs": [],
        "source": "actionq-daemon",
        "metadata": {
            "action_id": 1,
            "session_id": "session-1",
            "runtime_session_id": "session-1",
            "phase": "terminal",
            "terminal_status": "completed",
            "terminal_reason": "completed",
            "dispatch_result_ref": "artifact:sha256:" + "c" * 64,
            "dispatch_result_digest": "sha256:" + "c" * 64,
        },
        "created_at": "2026-04-26T10:00:01Z",
    }
    assert validate_event_object(event) == event

    with pytest.raises(ValueError, match="missing:.*dispatch_result_digest"):
        validate_event_object({
            **event,
            "metadata": {"dispatch_result_ref": "artifact:sha256:" + "c" * 64},
        })
    with pytest.raises(ValueError, match="recognized safe reason code"):
        validate_event_object({
            **event,
            "metadata": {**event["metadata"], "terminal_reason": "/tmp/secret.txt"},
        })
    with pytest.raises(ValueError, match="same result"):
        validate_event_object({
            **event,
            "metadata": {
                **event["metadata"],
                "dispatch_result_digest": "sha256:" + "d" * 64,
            },
        })
    with pytest.raises(ValueError, match="32-byte bound"):
        validate_event_object({
            **event,
            "metadata": {**event["metadata"], "phase": "p" * 33},
        })


def test_observation_envelope_maps_stable_audit_fields() -> None:
    event = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "tester",
        "summary": "Mapped",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {"runtime_session_id": "session-1", "basis_revision": "item:active"},
        "created_at": "2026-04-26T10:00:01Z",
    }
    enveloped = with_observation_envelope(
        event,
        origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
        origin_seq=3,
    )

    assert validate_event_object(enveloped) == enveloped
    assert enveloped["event_id"] == event["id"]
    assert enveloped["event_type"] == event["type"]
    assert enveloped["occurred_at"] == event["ts"]
    assert enveloped["runtime_session_id"] == "session-1"
    assert enveloped["basis_revision"] == "item:active"
    assert enveloped["payload"]["summary"] == "Mapped"


def test_observation_envelope_rejects_digest_or_identity_drift() -> None:
    event = with_observation_envelope(
        {
            "id": "ad:01HWXYZ0000000000000000000",
            "ts": "2026-04-26T10:00:00Z",
            "type": "decision",
            "actor": "tester",
            "summary": "Mapped",
            "detail": None,
            "refs": [],
            "source": "test",
            "metadata": {},
            "created_at": "2026-04-26T10:00:01Z",
        },
        origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
        origin_seq=1,
    )
    with pytest.raises(ValueError, match="event_id must match"):
        validate_event_object({**event, "event_id": "ad:01HWXYZ0000000000000000001"})
    with pytest.raises(ValueError, match="payload does not match"):
        validate_event_object({**event, "summary": "changed"})
    with pytest.raises(ValueError, match="payload_sha256 does not match"):
        validate_event_object({**event, "payload_sha256": "0" * 64})
