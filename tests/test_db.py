from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from auditctl import db
from auditctl.validation import with_observation_envelope


def test_init_db_creates_schema(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        db.init_db(conn)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 2
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"schema_version", "audit_event", "producer_stream"} <= tables
    finally:
        conn.close()


def test_origin_allocation_rolls_back_with_the_caller_transaction(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        db.init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        first = db.allocate_origin(conn)
        conn.rollback()

        conn.execute("BEGIN IMMEDIATE")
        retried = db.allocate_origin(conn)
        conn.commit()

        assert retried[1] == 1
        assert first[1] == 1
    finally:
        conn.close()


def test_v1_migration_preserves_legacy_event(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "legacy.db")
    legacy = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "legacy",
        "summary": "Before envelopes",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {},
        "created_at": "2026-04-26T10:00:01Z",
    }
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        db._migration_1(conn)
        conn.execute(
            "INSERT INTO audit_event "
            "(id, ts, type, actor, summary, detail, refs, source, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                legacy["id"],
                legacy["ts"],
                legacy["type"],
                legacy["actor"],
                legacy["summary"],
                legacy["detail"],
                "[]",
                legacy["source"],
                "{}",
                legacy["created_at"],
            ),
        )
        conn.commit()

        db.init_db(conn)

        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert db.query_events(conn) == [legacy]
    finally:
        conn.close()


def test_import_rejects_origin_tuple_reuse_by_a_different_event(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "auditctl.db")
    base = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "tester",
        "summary": "First",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {},
        "created_at": "2026-04-26T10:00:01Z",
    }
    first = with_observation_envelope(
        base,
        origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
        origin_seq=1,
    )
    conflicting = with_observation_envelope(
        {**base, "id": "ad:01HWXYZ0000000000000000001", "summary": "Conflict"},
        origin_stream_id=first["origin_stream_id"],
        origin_seq=first["origin_seq"],
    )
    try:
        db.init_db(conn)
        with pytest.raises(sqlite3.IntegrityError, match="origin_stream_id, audit_event.origin_seq"):
            db.import_events(conn, [first, conflicting])
        assert db.query_events(conn) == []
    finally:
        conn.close()


def test_insert_and_query_round_trip(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        db.init_db(conn)
        event = {
            "id": "ad:01HWXYZ0000000000000000000",
            "ts": "2026-04-26T10:00:00Z",
            "type": "decision",
            "actor": "tester",
            "summary": "Test event",
            "detail": None,
            "refs": ["sha:abc"],
            "source": "test",
            "metadata": {"x": 1},
            "created_at": "2026-04-26T10:00:01Z",
        }
        conn.execute("BEGIN IMMEDIATE")
        db.insert_event(conn, event)
        conn.commit()
        rows = db.query_events(conn)
        assert rows == [event]
    finally:
        conn.close()
