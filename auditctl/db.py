from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .validation import canonical_json


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.execute(
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
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_event_ts_type ON audit_event(ts, type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_event_type_ts ON audit_event(type, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_event_source_ts ON audit_event(source, ts)"
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    for column, column_type in (
        ("origin_stream_id", "TEXT"),
        ("origin_seq", "INTEGER"),
        ("schema_version", "INTEGER"),
        ("record_class", "TEXT"),
        ("runtime_session_id", "TEXT"),
        ("basis_revision", "TEXT"),
        ("correlation_id", "TEXT"),
        ("causation_id", "TEXT"),
        ("payload_sha256", "TEXT"),
    ):
        conn.execute(f"ALTER TABLE audit_event ADD COLUMN {column} {column_type}")
    conn.execute(
        "CREATE UNIQUE INDEX idx_audit_event_origin "
        "ON audit_event(origin_stream_id, origin_seq) "
        "WHERE origin_stream_id IS NOT NULL AND origin_seq IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE producer_stream (
            singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),
            origin_stream_id TEXT NOT NULL UNIQUE,
            next_origin_seq  INTEGER NOT NULL CHECK (next_origin_seq > 0)
        )
        """
    )


_MIGRATIONS = [_migration_1, _migration_2]

_WAL_BUSY_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(len(_WAL_BUSY_RETRY_DELAYS_SECONDS) + 1):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except Exception as exc:
            conn.close()
            is_busy = (
                isinstance(exc, sqlite3.OperationalError)
                and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY
            )
            if not is_busy or attempt == len(_WAL_BUSY_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(_WAL_BUSY_RETRY_DELAYS_SECONDS[attempt])
            continue
        return conn
    raise AssertionError("unreachable WAL initialization retry state")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version VALUES (0)")
            current = 0
        else:
            current = int(row[0])
        for index, migration in enumerate(_MIGRATIONS, start=1):
            if current < index:
                migration(conn)
                conn.execute("UPDATE schema_version SET version = ?", (index,))
                current = index
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO audit_event
            (id, ts, type, actor, summary, detail, refs, source, metadata, created_at,
             origin_stream_id, origin_seq, schema_version, record_class,
             runtime_session_id, basis_revision, correlation_id, causation_id,
             payload_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            event.get("origin_stream_id"),
            event.get("origin_seq"),
            event.get("schema_version"),
            event.get("record_class"),
            event.get("runtime_session_id"),
            event.get("basis_revision"),
            event.get("correlation_id"),
            event.get("causation_id"),
            event.get("payload_sha256"),
        ),
    )


def insert_event_ignore(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    cur = conn.execute(
        """
        INSERT INTO audit_event
            (id, ts, type, actor, summary, detail, refs, source, metadata, created_at,
             origin_stream_id, origin_seq, schema_version, record_class,
             runtime_session_id, basis_revision, correlation_id, causation_id,
             payload_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
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
            event.get("origin_stream_id"),
            event.get("origin_seq"),
            event.get("schema_version"),
            event.get("record_class"),
            event.get("runtime_session_id"),
            event.get("basis_revision"),
            event.get("correlation_id"),
            event.get("causation_id"),
            event.get("payload_sha256"),
        ),
    )
    return cur.rowcount == 1


def allocate_origin(conn: sqlite3.Connection) -> tuple[str, int]:
    """Allocate within the caller's SQLite writer transaction."""
    row = conn.execute(
        "SELECT origin_stream_id, next_origin_seq FROM producer_stream WHERE singleton = 1"
    ).fetchone()
    if row is None:
        origin_stream_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO producer_stream (singleton, origin_stream_id, next_origin_seq) "
            "VALUES (1, ?, 2)",
            (origin_stream_id,),
        )
        return origin_stream_id, 1
    origin_stream_id = str(row["origin_stream_id"])
    origin_seq = int(row["next_origin_seq"])
    conn.execute(
        "UPDATE producer_stream SET next_origin_seq = ? WHERE singleton = 1",
        (origin_seq + 1,),
    )
    return origin_stream_id, origin_seq


def observe_origin(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    origin_stream_id = event.get("origin_stream_id")
    origin_seq = event.get("origin_seq")
    if origin_stream_id is None or origin_seq is None:
        return
    row = conn.execute(
        "SELECT origin_stream_id, next_origin_seq FROM producer_stream WHERE singleton = 1"
    ).fetchone()
    next_origin_seq = int(origin_seq) + 1
    if row is None:
        conn.execute(
            "INSERT INTO producer_stream (singleton, origin_stream_id, next_origin_seq) "
            "VALUES (1, ?, ?)",
            (origin_stream_id, next_origin_seq),
        )
        return
    if row["origin_stream_id"] != origin_stream_id:
        raise ValueError(
            "NDJSON contains a different producer origin stream than the local audit ledger"
        )
    if int(row["next_origin_seq"]) < next_origin_seq:
        conn.execute(
            "UPDATE producer_stream SET next_origin_seq = ? WHERE singleton = 1",
            (next_origin_seq,),
        )


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    event = {
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
    if row["origin_stream_id"] is not None:
        event.update(
            {
                "event_id": row["id"],
                "schema_version": row["schema_version"],
                "record_class": row["record_class"],
                "origin_stream_id": row["origin_stream_id"],
                "origin_seq": row["origin_seq"],
                "event_type": row["type"],
                "runtime_session_id": row["runtime_session_id"],
                "occurred_at": row["ts"],
                "basis_revision": row["basis_revision"],
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
                "payload": {
                    "summary": row["summary"],
                    "detail": row["detail"],
                    "refs": json.loads(row["refs"] or "[]"),
                    "source": row["source"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                },
                "payload_sha256": row["payload_sha256"],
            }
        )
    return event


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
            observe_origin(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return imported, skipped
