from __future__ import annotations

from pathlib import Path

from auditctl import db


def test_init_db_creates_schema(repo_root: Path) -> None:
    conn = db.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        db.init_db(conn)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 1
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"schema_version", "audit_event"} <= tables
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

