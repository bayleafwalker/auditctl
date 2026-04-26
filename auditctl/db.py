from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .validation import canonical_json


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_event (
            id          TEXT PRIMARY KEY,
            ts          TEXT NOT NULL,
            type        TEXT NOT NULL,
            actor       TEXT NOT NULL,
            summary     TEXT NOT NULL,
            detail      TEXT,
            refs        TEXT NOT NULL DEFAULT '[]',
            source      TEXT NOT NULL,
            metadata    TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        CREATE INDEX IF NOT EXISTS idx_audit_event_ts_type
            ON audit_event(ts, type);

        CREATE INDEX IF NOT EXISTS idx_audit_event_type_ts
            ON audit_event(type, ts);

        CREATE INDEX IF NOT EXISTS idx_audit_event_source_ts
            ON audit_event(source, ts);
        """
    )


_MIGRATIONS = [_migration_1]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        current = 0
    else:
        current = int(row[0])
    conn.commit()

    for index, migration in enumerate(_MIGRATIONS, start=1):
        if current < index:
            conn.execute("BEGIN IMMEDIATE")
            try:
                migration(conn)
                conn.execute("UPDATE schema_version SET version = ?", (index,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            current = index


def insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO audit_event
            (id, ts, type, actor, summary, detail, refs, source, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["id"],
            event["ts"],
            event["type"],
            event["actor"],
            event["summary"],
            event.get("detail"),
            canonical_json(event["refs"]),
            event["source"],
            canonical_json(event["metadata"]),
            event["created_at"],
        ),
    )


def insert_event_ignore(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO audit_event
            (id, ts, type, actor, summary, detail, refs, source, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["id"],
            event["ts"],
            event["type"],
            event["actor"],
            event["summary"],
            event.get("detail"),
            canonical_json(event["refs"]),
            event["source"],
            canonical_json(event["metadata"]),
            event["created_at"],
        ),
    )
    return cur.rowcount == 1


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "type": row["type"],
        "actor": row["actor"],
        "summary": row["summary"],
        "detail": row["detail"],
        "refs": json.loads(row["refs"] or "[]"),
        "source": row["source"],
        "metadata": json.loads(row["metadata"] or "{}"),
        "created_at": row["created_at"],
    }


def query_events(
    conn: sqlite3.Connection,
    *,
    type_: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = 50,
    ascending: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if type_:
        where.append("type = ?")
        params.append(type_)
    if source:
        where.append("source = ?")
        params.append(source)
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)

    order = "ASC" if ascending else "DESC"
    sql = "SELECT * FROM audit_event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY ts {order}, id {order}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_event(row) for row in conn.execute(sql, params).fetchall()]


def import_events(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> tuple[int, int]:
    imported = 0
    skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for event in events:
            if insert_event_ignore(conn, event):
                imported += 1
            else:
                skipped += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return imported, skipped
