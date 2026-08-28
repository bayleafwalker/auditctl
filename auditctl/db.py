from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .validation import canonical_json


class ImportValidationError(ValueError):
    """A typed, fail-closed rejection of an NDJSON import batch."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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


def _canonical_import_record(event: dict[str, Any]) -> str:
    """The complete record representation that the local ledger persists."""
    record = {
        "id": event["id"],
        "ts": event["ts"],
        "type": event["type"],
        "actor": event["actor"],
        "summary": event["summary"],
        "detail": event.get("detail"),
        "refs": event["refs"],
        "source": event["source"],
        "metadata": event["metadata"],
        "created_at": event["created_at"],
    }
    if event.get("origin_stream_id") is not None:
        record.update(
            {
                key: event[key]
                for key in (
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
                )
            }
        )
    return canonical_json(record)


def _deduplicate_batch(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only exact canonical retries; reject every identity collision."""
    unique: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    for event in events:
        canonical = _canonical_import_record(event)
        previous = by_id.get(event["id"])
        if previous is None:
            by_id[event["id"]] = canonical
            unique.append(event)
        elif previous != canonical:
            raise ImportValidationError(
                "incompatible_duplicate_identity",
                f"event_id {event['id']} has more than one canonical record",
            )
    return unique


def _validate_import_batch(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the complete import before inserting a row or advancing an origin."""
    unique = _deduplicate_batch(events)
    existing: dict[str, dict[str, Any]] = {}
    for event in unique:
        row = conn.execute("SELECT * FROM audit_event WHERE id = ?", (event["id"],)).fetchone()
        if row is not None:
            existing[event["id"]] = _row_to_event(row)
            if _canonical_import_record(existing[event["id"]]) != _canonical_import_record(event):
                raise ImportValidationError(
                    "incompatible_duplicate_identity",
                    f"event_id {event['id']} does not match the persisted canonical record",
                )

    stream = conn.execute(
        "SELECT origin_stream_id, next_origin_seq FROM producer_stream WHERE singleton = 1"
    ).fetchone()
    stream_id = str(stream["origin_stream_id"]) if stream is not None else None
    expected_seq = int(stream["next_origin_seq"]) if stream is not None else 1
    for event in unique:
        if event.get("origin_stream_id") is None:
            continue  # Explicitly supported legacy, no-envelope records.
        if stream_id is not None and event["origin_stream_id"] != stream_id:
            raise ImportValidationError(
                "origin_discontinuity",
                "NDJSON origin stream differs from the local audit ledger",
            )
        if event["id"] in existing:
            continue  # Exact canonical retry; it cannot advance the cursor.
        if event["origin_seq"] != expected_seq:
            raise ImportValidationError(
                "origin_discontinuity",
                f"expected origin_seq {expected_seq}, got {event['origin_seq']}",
            )
        stream_id = str(event["origin_stream_id"])
        expected_seq += 1
    return unique


def validate_import_batch(
    db_path: Path, events: Iterable[dict[str, Any]], *, against_existing: bool = True
) -> None:
    """Read-only preflight used before rebuild may replace its destination DB."""
    unique = _deduplicate_batch(events)
    if not against_existing or not db_path.exists():
        # Validate against an empty ledger without creating the destination.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            _validate_import_batch(conn, unique)
        finally:
            conn.close()
        return
    # immutable=1 prevents SQLite from creating WAL/SHM sidecars during the
    # rejection preflight.  The subsequent writer transaction revalidates to
    # close the ordinary local-writer race.
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        _validate_import_batch(conn, unique)
    except sqlite3.Error as exc:
        raise ImportValidationError("destination_unreadable", str(exc)) from exc
    finally:
        conn.close()


def index_only_events(
    db_path: Path, events: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return persisted events the incoming shard batch does not carry.

    Shards are the authoritative record and the sqlite index is derived, so
    an event that exists only in the index is either a lost shard or a
    publisher that indexed without appending.  Both are silent data loss the
    moment anyone rebuilds, and neither is visible to the batch validation
    above, which only ever inspects the ids the batch itself names.
    """

    if not db_path.exists():
        return []
    batch_ids = {event["id"] for event in _deduplicate_batch(events)}
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, ts, type, source FROM audit_event ORDER BY ts, id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise ImportValidationError("destination_unreadable", str(exc)) from exc
    finally:
        conn.close()
    return [dict(row) for row in rows if row["id"] not in batch_ids]


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
    batch = list(events)
    imported = 0
    skipped = len(batch) - len(_deduplicate_batch(batch))
    conn.execute("BEGIN IMMEDIATE")
    try:
        unique = _validate_import_batch(conn, batch)
        existing_ids = {
            event["id"]
            for event in unique
            if conn.execute("SELECT 1 FROM audit_event WHERE id = ?", (event["id"],)).fetchone()
            is not None
        }
        for event in unique:
            if event["id"] in existing_ids:
                skipped += 1
                continue
            if insert_event_ignore(conn, event):
                imported += 1
            else:
                raise AssertionError("validated import unexpectedly lost an event identity race")
            observe_origin(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return imported, skipped
