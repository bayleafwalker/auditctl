from __future__ import annotations

from types import SimpleNamespace

import pytest

from auditctl.central import (
    MAX_READ_LIMIT,
    _is_event_id_unique_violation,
    prepare_observation,
)
from auditctl.central_schema import (
    CURRENT_SCHEMA_VERSION,
    load_migrations,
    migrate,
)
from auditctl.validation import with_observation_envelope


def _event() -> dict:
    return with_observation_envelope(
        {
            "id": "ad:00000000000000000000000001",
            "ts": "2026-07-21T12:00:00Z",
            "type": "session.started",
            "actor": "codex:test",
            "summary": "Session started",
            "detail": None,
            "refs": [],
            "source": "test",
            "metadata": {"runtime_session_id": "session-1"},
            "created_at": "2026-07-21T12:00:01Z",
        },
        origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
        origin_seq=1,
    )


def test_central_migration_assets_are_contiguous_and_immutable_inputs() -> None:
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == list(
        range(1, CURRENT_SCHEMA_VERSION + 1)
    )
    assert all(len(migration.sha256) == 64 for migration in migrations)
    assert "__SCHEMA__" in migrations[0].sql
    assert "record_class = 'observation'" in migrations[0].sql
    assert "ingest_receipt" in migrations[1].sql


def test_prepare_observation_preserves_origin_and_produces_stable_record_hash() -> None:
    event = _event()

    first = prepare_observation(event)
    second = prepare_observation(dict(event))

    assert first == second
    assert first.origin_stream_id == event["origin_stream_id"]
    assert first.origin_seq == 1
    assert first.event_id == event["id"]
    assert len(first.record_sha256) == 64


def test_prepare_observation_rejects_authority_classes() -> None:
    event = _event()
    event["record_class"] = "authority-command"

    with pytest.raises(ValueError, match="record_class must be observation"):
        prepare_observation(event)


def test_read_limit_is_deliberately_bounded() -> None:
    assert MAX_READ_LIMIT == 100


def test_migration_and_runtime_roles_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="must be different"):
        migrate(
            None,
            schema="audit",
            migration_role="same_role",
            runtime_role="same_role",
        )


def test_only_the_owned_event_id_constraint_is_translated() -> None:
    event_conflict = RuntimeError("event conflict")
    event_conflict.sqlstate = "23505"  # type: ignore[attr-defined]
    event_conflict.diag = SimpleNamespace(  # type: ignore[attr-defined]
        constraint_name="ingest_observation_event_id_key"
    )
    origin_conflict = RuntimeError("origin conflict")
    origin_conflict.sqlstate = "23505"  # type: ignore[attr-defined]
    origin_conflict.diag = SimpleNamespace(  # type: ignore[attr-defined]
        constraint_name="ingest_observation_origin_seq_key"
    )

    assert _is_event_id_unique_violation(event_conflict)
    assert not _is_event_id_unique_violation(origin_conflict)
