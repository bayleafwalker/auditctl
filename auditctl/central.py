from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .central_schema import require_runtime_compatibility
from .validation import RECORD_CLASSES, canonical_payload_json, validate_event_object

MAX_READ_LIMIT = 100
EVENT_ID_LOCK_NAMESPACE = "auditctl-central-event-id"


class IngestError(RuntimeError):
    """Base error for central observation ingestion."""


class IngestConflictError(IngestError):
    """A stable producer or event identity was reused for different content."""


class IngestGapError(IngestError):
    """An origin sequence arrived ahead of the stream's contiguous cursor."""

    def __init__(self, origin_stream_id: str, expected: int, received: int) -> None:
        super().__init__(
            f"origin stream {origin_stream_id} expected sequence {expected}, received {received}"
        )
        self.origin_stream_id = origin_stream_id
        self.expected = expected
        self.received = received


@dataclass(frozen=True)
class PreparedObservation:
    origin_stream_id: str
    origin_seq: int
    event_id: str
    schema_version: int
    record_class: str
    event_type: str
    actor: str
    runtime_session_id: str | None
    occurred_at: str
    basis_revision: str | None
    correlation_id: str | None
    causation_id: str | None
    payload: dict[str, Any]
    payload_sha256: str
    record_sha256: str
    producer_created_at: str


@dataclass(frozen=True)
class IngestReceipt:
    receipt_id: str
    ingest_offset: int
    origin_stream_id: str
    origin_seq: int
    event_id: str
    record_sha256: str
    first_received_at: str
    last_seen_at: str
    duplicate_count: int
    duplicate: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StreamStatus:
    origin_stream_id: str
    highest_contiguous_seq: int
    next_expected_seq: int


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def prepare_observation(event: dict[str, Any]) -> PreparedObservation:
    """Validate a local audit envelope and produce its central immutable hash."""
    validate_event_object(event)
    if event.get("record_class") not in RECORD_CLASSES:
        raise ValueError(
            "central audit ingestion accepts these record classes only: "
            + ", ".join(RECORD_CLASSES)
        )
    if event.get("schema_version") != 1:
        raise ValueError(
            "central audit ingestion supports observation schema version 1"
        )
    origin_stream_id = _required_text(event.get("origin_stream_id"), "origin_stream_id")
    try:
        uuid.UUID(origin_stream_id)
    except ValueError as exc:
        raise ValueError("origin_stream_id must be a UUID") from exc
    origin_seq = event.get("origin_seq")
    if (
        isinstance(origin_seq, bool)
        or not isinstance(origin_seq, int)
        or origin_seq < 1
    ):
        raise ValueError("origin_seq must be a positive integer")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    payload_json = canonical_payload_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if event.get("payload_sha256") != payload_sha256:
        raise ValueError("payload_sha256 does not match the canonical payload")

    canonical_record = {
        "origin_stream_id": origin_stream_id,
        "origin_seq": origin_seq,
        "event_id": _required_text(event.get("event_id"), "event_id"),
        "schema_version": 1,
        # Must be the record's own class, not a constant. The digest below is this
        # record's immutable identity, and a judgement that hashed identically to an
        # observation with the same fields would make the two indistinguishable at
        # exactly the layer that exists to tell records apart. Observations still
        # hash to what they always did, so no existing digest changes.
        "record_class": event["record_class"],
        "event_type": _required_text(event.get("event_type"), "event_type"),
        "actor": _required_text(event.get("actor"), "actor"),
        "runtime_session_id": _optional_text(
            event.get("runtime_session_id"), "runtime_session_id"
        ),
        "occurred_at": _required_text(event.get("occurred_at"), "occurred_at"),
        "basis_revision": _optional_text(event.get("basis_revision"), "basis_revision"),
        "correlation_id": _optional_text(event.get("correlation_id"), "correlation_id"),
        "causation_id": _optional_text(event.get("causation_id"), "causation_id"),
        "payload": payload,
        "payload_sha256": payload_sha256,
        "producer_created_at": _required_text(event.get("created_at"), "created_at"),
    }
    record_json = json.dumps(
        canonical_record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return PreparedObservation(
        **canonical_record,
        record_sha256=hashlib.sha256(record_json.encode("utf-8")).hexdigest(),
    )


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _receipt_from_row(row: dict[str, Any], *, duplicate: bool) -> IngestReceipt:
    return IngestReceipt(
        receipt_id=str(row["receipt_id"]),
        ingest_offset=int(row["ingest_offset"]),
        origin_stream_id=str(row["origin_stream_id"]),
        origin_seq=int(row["origin_seq"]),
        event_id=str(row["event_id"]),
        record_sha256=str(row["record_sha256"]),
        first_received_at=_iso(row["first_received_at"]),
        last_seen_at=_iso(row["last_seen_at"]),
        duplicate_count=int(row["duplicate_count"]),
        duplicate=duplicate,
    )


def _select_receipt_for_update(
    cur: Any, *, schema: str, origin_stream_id: str, origin_seq: int
) -> dict[str, Any] | None:
    cur.execute(
        f"""
        SELECT r.*, o.ingest_offset
        FROM "{schema}".ingest_receipt AS r
        JOIN "{schema}".ingest_observation AS o USING (observation_id)
        WHERE r.origin_stream_id = %s AND r.origin_seq = %s
        FOR UPDATE OF r
        """,
        (origin_stream_id, origin_seq),
    )
    return cur.fetchone()


def _is_event_id_unique_violation(exc: BaseException) -> bool:
    """Recognize only the database constraint owned by the event-ID contract."""
    diag = getattr(exc, "diag", None)
    return (
        getattr(exc, "sqlstate", None) == "23505"
        and getattr(diag, "constraint_name", None) == "ingest_observation_event_id_key"
    )


def ingest_observation(
    conn: Any, event: dict[str, Any], *, schema: str
) -> IngestReceipt:
    """Atomically ingest one contiguous observation and return a stable receipt.

    Exact retries retain the original receipt identity. Changed content under
    an existing origin tuple or event ID is rejected. A gap admits no partial
    state and reports the next expected sequence.
    """
    item = prepare_observation(event)
    with conn.transaction():
        require_runtime_compatibility(conn, schema=schema)
        with conn.cursor() as cur:
            # Event IDs are globally unique inside one environment schema. Lock
            # before touching a stream so two distinct streams cannot both pass
            # the event-ID precheck and leak a driver-specific unique error.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{EVENT_ID_LOCK_NAMESPACE}:{schema}:{item.event_id}",),
            )
            cur.execute(
                f"""
                INSERT INTO "{schema}".ingest_stream (origin_stream_id)
                VALUES (%s)
                ON CONFLICT (origin_stream_id) DO NOTHING
                """,
                (item.origin_stream_id,),
            )
            cur.execute(
                f"""
                SELECT highest_contiguous_seq
                FROM "{schema}".ingest_stream
                WHERE origin_stream_id = %s
                FOR UPDATE
                """,
                (item.origin_stream_id,),
            )
            stream = cur.fetchone()
            assert stream is not None
            highest = int(stream["highest_contiguous_seq"])

            existing = _select_receipt_for_update(
                cur,
                schema=schema,
                origin_stream_id=item.origin_stream_id,
                origin_seq=item.origin_seq,
            )
            if existing is not None:
                if existing["record_sha256"] != item.record_sha256:
                    raise IngestConflictError(
                        "origin stream sequence already identifies different content"
                    )
                cur.execute(
                    f"""
                    UPDATE "{schema}".ingest_receipt
                    SET duplicate_count = duplicate_count + 1,
                        last_seen_at = clock_timestamp()
                    WHERE receipt_id = %s
                    RETURNING *
                    """,
                    (existing["receipt_id"],),
                )
                receipt = cur.fetchone()
                assert receipt is not None
                receipt["ingest_offset"] = existing["ingest_offset"]
                return _receipt_from_row(receipt, duplicate=True)

            cur.execute(
                f'SELECT record_sha256 FROM "{schema}".ingest_observation WHERE event_id = %s',
                (item.event_id,),
            )
            same_event = cur.fetchone()
            if same_event is not None:
                raise IngestConflictError(
                    "event_id already identifies a different origin record"
                )

            expected = highest + 1
            if item.origin_seq != expected:
                raise IngestGapError(item.origin_stream_id, expected, item.origin_seq)

            try:
                cur.execute(
                    f"""
                    INSERT INTO "{schema}".ingest_observation (
                        origin_stream_id, origin_seq, event_id, schema_version,
                        record_class, event_type, actor, runtime_session_id,
                        occurred_at, basis_revision, correlation_id, causation_id,
                        payload, payload_sha256, record_sha256, producer_created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s::jsonb, %s, %s, %s
                    )
                    RETURNING observation_id, ingest_offset, ingested_at
                    """,
                    (
                        item.origin_stream_id,
                        item.origin_seq,
                        item.event_id,
                        item.schema_version,
                        item.record_class,
                        item.event_type,
                        item.actor,
                        item.runtime_session_id,
                        item.occurred_at,
                        item.basis_revision,
                        item.correlation_id,
                        item.causation_id,
                        canonical_payload_json(item.payload),
                        item.payload_sha256,
                        item.record_sha256,
                        item.producer_created_at,
                    ),
                )
            except Exception as exc:
                if _is_event_id_unique_violation(exc):
                    raise IngestConflictError(
                        "event_id already identifies a different origin record"
                    ) from exc
                raise
            observation = cur.fetchone()
            assert observation is not None
            cur.execute(
                f"""
                INSERT INTO "{schema}".ingest_receipt (
                    observation_id, origin_stream_id, origin_seq, event_id,
                    record_sha256, first_received_at, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    observation["observation_id"],
                    item.origin_stream_id,
                    item.origin_seq,
                    item.event_id,
                    item.record_sha256,
                    observation["ingested_at"],
                    observation["ingested_at"],
                ),
            )
            receipt = cur.fetchone()
            assert receipt is not None
            cur.execute(
                f"""
                UPDATE "{schema}".ingest_stream
                SET highest_contiguous_seq = %s, updated_at = clock_timestamp()
                WHERE origin_stream_id = %s
                """,
                (item.origin_seq, item.origin_stream_id),
            )
            receipt["ingest_offset"] = observation["ingest_offset"]
            return _receipt_from_row(receipt, duplicate=False)


def get_receipt(
    conn: Any,
    *,
    schema: str,
    receipt_id: str | None = None,
    event_id: str | None = None,
) -> IngestReceipt | None:
    require_runtime_compatibility(conn, schema=schema)
    if (receipt_id is None) == (event_id is None):
        raise ValueError("provide exactly one of receipt_id or event_id")
    field = "receipt_id" if receipt_id is not None else "event_id"
    value = receipt_id if receipt_id is not None else event_id
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.*, o.ingest_offset
            FROM "{schema}".ingest_receipt AS r
            JOIN "{schema}".ingest_observation AS o USING (observation_id)
            WHERE r.{field} = %s
            """,
            (value,),
        )
        row = cur.fetchone()
    return None if row is None else _receipt_from_row(row, duplicate=False)


def get_stream_status(conn: Any, *, schema: str, origin_stream_id: str) -> StreamStatus:
    require_runtime_compatibility(conn, schema=schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT highest_contiguous_seq
            FROM "{schema}".ingest_stream
            WHERE origin_stream_id = %s
            """,
            (origin_stream_id,),
        )
        row = cur.fetchone()
    highest = 0 if row is None else int(row["highest_contiguous_seq"])
    return StreamStatus(origin_stream_id, highest, highest + 1)


def list_observations(
    conn: Any,
    *,
    schema: str,
    after_offset: int = 0,
    limit: int = 50,
    event_type: str | None = None,
    origin_stream_id: str | None = None,
) -> list[dict[str, Any]]:
    require_runtime_compatibility(conn, schema=schema)
    if (
        isinstance(after_offset, bool)
        or not isinstance(after_offset, int)
        or after_offset < 0
    ):
        raise ValueError("after_offset must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_READ_LIMIT
    ):
        raise ValueError(f"limit must be between 1 and {MAX_READ_LIMIT}")
    where = ["o.ingest_offset > %s"]
    params: list[Any] = [after_offset]
    if event_type is not None:
        where.append("o.event_type = %s")
        params.append(_required_text(event_type, "event_type"))
    if origin_stream_id is not None:
        where.append("o.origin_stream_id = %s")
        params.append(origin_stream_id)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                o.ingest_offset, o.origin_stream_id::text, o.origin_seq,
                o.event_id, o.schema_version, o.record_class, o.event_type,
                o.actor, o.runtime_session_id, o.occurred_at,
                o.basis_revision, o.correlation_id, o.causation_id,
                o.payload, o.payload_sha256, o.record_sha256,
                o.producer_created_at, o.ingested_at, r.receipt_id::text
            FROM "{schema}".ingest_observation AS o
            JOIN "{schema}".ingest_receipt AS r USING (observation_id)
            WHERE {" AND ".join(where)}
            ORDER BY o.ingest_offset ASC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]
