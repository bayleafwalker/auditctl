from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditctl.central import (
    MAX_READ_LIMIT,
    _is_event_id_unique_violation,
    prepare_observation,
)
from auditctl.central_schema import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationDriftError,
    _validate_applied_migrations,
    load_migrations,
    migrate,
)
from auditctl.validation import with_observation_envelope
from vuoro_schema_runtime import MigrationAsset, render_schema_sql


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


def test_shared_runtime_preserves_exact_migration_asset_bytes_and_digests() -> None:
    migrations = load_migrations()
    asset_root = (
        Path(__file__).parents[1]
        / "auditctl"
        / "central_migrations"
        / "versions"
    )

    assert Migration is MigrationAsset
    assert all(isinstance(migration, MigrationAsset) for migration in migrations)
    assert [len(migration.sql.encode("utf-8")) for migration in migrations] == [
        1946,
        1740,
    ]
    assert [migration.sha256 for migration in migrations] == [
        "1f6aca04414ca90a41fda2bb894b0d4f9b5c937979fb677139515fdc25ed52be",
        "66db0101dccde630400ae4e954aa9999ba9e57361dc07a0787ed40eec9a40794",
    ]
    assert all(
        migration.sha256
        == hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
        for migration in migrations
    )
    assert [migration.sql.encode("utf-8") for migration in migrations] == [
        asset.read_bytes() for asset in sorted(asset_root.glob("*.sql"))
    ]


@pytest.mark.parametrize("schema", ["audit", "vuoro_dev_audit", "a"])
def test_shared_runtime_rendering_is_byte_equivalent_to_local_substitution(
    schema: str,
) -> None:
    for migration in load_migrations():
        expected = migration.sql.replace("__SCHEMA__", f'"{schema}"')
        actual = render_schema_sql(migration.sql, schema)

        assert actual.encode("utf-8") == expected.encode("utf-8")
        assert "__SCHEMA__" not in actual


def test_shared_ledger_verdict_preserves_domain_error_contract() -> None:
    migrations = load_migrations()
    valid = {
        migration.version: (migration.name, migration.sha256)
        for migration in migrations
    }

    _validate_applied_migrations(migrations, valid)
    with pytest.raises(MigrationDriftError, match="versions are not contiguous"):
        _validate_applied_migrations(migrations, {2: valid[2]})
    with pytest.raises(MigrationDriftError, match="migration 1 checksum"):
        _validate_applied_migrations(
            migrations, {**valid, 1: (valid[1][0], "0" * 64)}
        )
    with pytest.raises(MigrationDriftError, match="version 3 is newer"):
        _validate_applied_migrations(migrations, {**valid, 3: ("future", "3" * 64)})


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
