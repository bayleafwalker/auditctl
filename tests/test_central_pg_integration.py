from __future__ import annotations

import getpass
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

import pytest
from click.testing import CliRunner

from auditctl import db
from auditctl.central import (
    IngestConflictError,
    IngestGapError,
    get_receipt,
    get_stream_status,
    ingest_observation,
    list_observations,
    prepare_observation,
)
from auditctl.cli import cli
from auditctl.central_schema import (
    MigrationDriftError,
    SchemaCompatibilityError,
    check_compatibility,
    migrate,
)
from auditctl.validation import with_observation_envelope
from auditctl.vuoro_adapter import VuoroAuditAdapter

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

MIGRATION_ROLE = "audit_migration"
RUNTIME_ROLE = "audit_runtime"
ROTATED_RUNTIME_ROLE = "audit_runtime_rotated"


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    if not all(shutil.which(command) for command in ("initdb", "pg_ctl")):
        pytest.skip(
            "PostgreSQL server binaries are required for central integration tests"
        )
    root = Path(tempfile.mkdtemp(prefix="auditctl-pg-"))
    data = root / "data"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    initdb = Path(shutil.which("initdb") or "initdb").resolve()
    pg_ctl = Path(shutil.which("pg_ctl") or "pg_ctl").resolve()
    initdb_args = [
        str(initdb),
        "--no-locale",
        "--encoding=UTF8",
        "--auth=trust",
        "-D",
        str(data),
    ]
    adjacent_share = initdb.parents[1] / "share" / "postgresql"
    if (adjacent_share / "postgres.bki").exists():
        initdb_args.extend(["-L", str(adjacent_share)])
    subprocess.run(
        initdb_args,
        check=True,
        capture_output=True,
        text=True,
    )
    options = f"-F -h '' -k {socket_dir} -p {port}"
    subprocess.run(
        [
            str(pg_ctl),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            options,
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    admin_dsn = (
        f"dbname=postgres user={getpass.getuser()} host={socket_dir} port={port}"
    )
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE ROLE {MIGRATION_ROLE} LOGIN")
                cur.execute(f"CREATE ROLE {RUNTIME_ROLE} LOGIN")
                cur.execute(f"CREATE ROLE {ROTATED_RUNTIME_ROLE} LOGIN")
                cur.execute(f"GRANT CREATE ON DATABASE postgres TO {MIGRATION_ROLE}")
        yield f"dbname=postgres host={socket_dir} port={port}"
    finally:
        subprocess.run(
            [str(pg_ctl), "-D", str(data), "-m", "fast", "-w", "stop"],
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def _connect(dsn: str, role: str) -> Iterator[Any]:
    with psycopg.connect(
        f"{dsn} user={role}", autocommit=True, row_factory=dict_row
    ) as conn:
        yield conn


def _schema(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _event(
    *,
    origin_stream_id: str,
    origin_seq: int,
    number: int,
    summary: str | None = None,
) -> dict[str, Any]:
    event_id = f"ad:{number:026d}"
    return with_observation_envelope(
        {
            "id": event_id,
            "ts": "2026-07-21T12:00:00Z",
            "type": "session.started",
            "actor": "codex:integration",
            "summary": summary or f"Observation {number}",
            "detail": None,
            "refs": [],
            "source": "integration-test",
            "metadata": {"runtime_session_id": f"session-{number}"},
            "created_at": "2026-07-21T12:00:01Z",
        },
        origin_stream_id=origin_stream_id,
        origin_seq=origin_seq,
    )


def _migrate_current(dsn: str, schema: str) -> None:
    with _connect(dsn, MIGRATION_ROLE) as conn:
        result = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        )
    assert result.installed_version == 2


class AdapterRejected(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _adapter_rejection(code: str, message: str, http_status: int) -> BaseException:
    return AdapterRejected(code, message, http_status)


def _adapter(dsn: str, schema: str) -> VuoroAuditAdapter:
    return VuoroAuditAdapter(
        connection_factory=lambda: _connect(dsn, RUNTIME_ROLE),
        schema=schema,
        rejection_factory=_adapter_rejection,
    )


def test_vuoro_adapter_retries_local_outbox_across_service_restart(
    postgres_dsn: str, repo_root: Path, tmp_path: Path
) -> None:
    local_add = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "session.started",
            "--actor",
            "codex:adapter-integration",
            "--summary",
            "Buffered before central service availability",
            "--source",
            "adapter-integration",
            "--ts",
            "2026-07-21T12:00:00Z",
            "--json",
        ],
    )
    assert local_add.exit_code == 0, local_add.output
    shard = (
        tmp_path
        / "_artifacts"
        / "example-repo"
        / "audit"
        / "events-2026-07-21.ndjson"
    )
    observation = json.loads(shard.read_text(encoding="utf-8").strip())

    schema = _schema("audit_adapter")
    _migrate_current(postgres_dsn, schema)
    context = SimpleNamespace(idempotency_key=observation["event_id"])
    first_adapter = _adapter(postgres_dsn, schema)

    # Treat the first return as a lost response. A fresh adapter instance uses
    # only the durable local observation and returns the original receipt.
    first_adapter.submit_observation({"observation": observation}, context)
    restarted_adapter = _adapter(postgres_dsn, schema)
    retried = restarted_adapter.submit_observation(
        {"observation": observation}, context
    )["receipt"]
    assert retried["duplicate"]
    assert retried["duplicate_count"] == 1

    lookup = restarted_adapter.lookup_receipt(
        {"event_id": observation["event_id"]}, SimpleNamespace()
    )["receipt"]
    assert lookup is not None
    assert lookup["receipt_id"] == retried["receipt_id"]
    rows = restarted_adapter.read_observations(
        {"after_offset": 0, "limit": 1}, SimpleNamespace()
    )
    assert rows["watermark"] == retried["ingest_offset"]
    assert rows["observations"][0]["event_id"] == observation["event_id"]
    assert rows["observations"][0]["ingested_at"].endswith("Z")

    status = restarted_adapter.stream_status(
        {"origin_stream_id": observation["origin_stream_id"]}, SimpleNamespace()
    )
    assert status["next_expected_seq"] == 2
    gap = _event(
        origin_stream_id=observation["origin_stream_id"],
        origin_seq=3,
        number=98,
    )
    with pytest.raises(AdapterRejected) as rejected:
        restarted_adapter.submit_observation(
            {"observation": gap},
            SimpleNamespace(idempotency_key=gap["event_id"]),
        )
    assert (rejected.value.code, rejected.value.http_status) == (
        "audit-origin-sequence-gap",
        409,
    )
    assert restarted_adapter.stream_status(
        {"origin_stream_id": observation["origin_stream_id"]}, SimpleNamespace()
    )["next_expected_seq"] == 2
    assert restarted_adapter.schema_compatibility({}, SimpleNamespace())[
        "compatible"
    ]

    # Local recovery remains independent of the central service and receipt.
    rebuild = CliRunner().invoke(
        cli, ["rebuild", "--from-ndjson", str(shard), "--replace"]
    )
    assert rebuild.exit_code == 0, rebuild.output
    local = db.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        assert [row["id"] for row in db.query_events(local, limit=None)] == [
            observation["id"]
        ]
    finally:
        local.close()


def test_empty_to_current_upgrade_backfills_receipts_and_is_idempotent(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_upgrade")
    stream_id = str(uuid4())
    event = _event(origin_stream_id=stream_id, origin_seq=1, number=10)
    prepared = prepare_observation(event)

    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        first = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            target_version=1,
        )
        assert first.applied_versions == (1,)
        old = check_compatibility(conn, schema=schema, expected_role_kind="migration")
        assert not old.compatible
        assert "schema_too_old" in old.reasons
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{schema}".ingest_stream '
                "(origin_stream_id, highest_contiguous_seq) VALUES (%s, 1)",
                (stream_id,),
            )
            cur.execute(
                f"""
                INSERT INTO "{schema}".ingest_observation (
                    origin_stream_id, origin_seq, event_id, schema_version,
                    record_class, event_type, actor, occurred_at, payload,
                    payload_sha256, record_sha256, producer_created_at
                ) VALUES (%s, %s, %s, 1, 'observation', %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    prepared.origin_stream_id,
                    prepared.origin_seq,
                    prepared.event_id,
                    prepared.event_type,
                    prepared.actor,
                    prepared.occurred_at,
                    psycopg.types.json.Jsonb(prepared.payload),
                    prepared.payload_sha256,
                    prepared.record_sha256,
                    prepared.producer_created_at,
                ),
            )
        upgraded = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        )
        repeated = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        )

    assert upgraded.applied_versions == (2,)
    assert repeated.applied_versions == ()
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        current = check_compatibility(conn, schema=schema)
        receipt = get_receipt(conn, schema=schema, event_id=event["id"])
    assert current.compatible
    assert receipt is not None
    assert receipt.origin_stream_id == stream_id


def test_migrations_are_serialized_and_checksum_drift_fails_closed(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_parallel")
    barrier = threading.Barrier(2)
    results: list[tuple[int, ...]] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
                barrier.wait()
                result = migrate(
                    conn,
                    schema=schema,
                    migration_role=MIGRATION_ROLE,
                    runtime_role=RUNTIME_ROLE,
                )
                results.append(result.applied_versions)
        except BaseException as exc:  # pragma: no cover - reported by assertion
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not failures
    assert sorted(results, key=len) == [(), (1, 2)]

    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE "{schema}".schema_migration SET sha256 = %s WHERE version = 1',
                ("0" * 64,),
            )
        with pytest.raises(MigrationDriftError, match="checksum"):
            migrate(
                conn,
                schema=schema,
                migration_role=MIGRATION_ROLE,
                runtime_role=RUNTIME_ROLE,
            )


def test_compatibility_is_read_only_and_future_schemas_fail_closed(
    postgres_dsn: str,
) -> None:
    absent_schema = _schema("audit_absent")
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        absent = check_compatibility(conn, schema=absent_schema)
    assert not absent.compatible
    assert absent.to_dict() == {
        "schema": absent_schema,
        "domain_api_version": "audit/v1",
        "installed_schema_version": None,
        "minimum_schema_version": 2,
        "maximum_schema_version": 2,
        "current_role": RUNTIME_ROLE,
        "expected_role_kind": "runtime",
        "configured_role": None,
        "compatible": False,
        "reasons": ["schema_not_initialized"],
    }

    future_schema = _schema("audit_future")
    _migrate_current(postgres_dsn, future_schema)
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{future_schema}".schema_migration (version, name, sha256) '
                "VALUES (3, 'future', %s)",
                ("3" * 64,),
            )
        with pytest.raises(MigrationDriftError, match="newer than this package"):
            migrate(
                conn,
                schema=future_schema,
                migration_role=MIGRATION_ROLE,
                runtime_role=RUNTIME_ROLE,
            )
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        future = check_compatibility(conn, schema=future_schema)
    assert future.to_dict() == {
        "schema": future_schema,
        "domain_api_version": "audit/v1",
        "installed_schema_version": 3,
        "minimum_schema_version": 2,
        "maximum_schema_version": 2,
        "current_role": RUNTIME_ROLE,
        "expected_role_kind": "runtime",
        "configured_role": None,
        "compatible": False,
        "reasons": ["schema_too_new"],
    }


def test_runtime_role_rotation_revokes_the_previous_principal(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_rotation")
    _migrate_current(postgres_dsn, schema)
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        result = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=ROTATED_RUNTIME_ROLE,
        )
    assert result.applied_versions == ()

    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        old = check_compatibility(conn, schema=schema)
        assert not old.compatible
        assert old.reasons == ("schema_access_denied",)
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f'SELECT 1 FROM "{schema}".ingest_stream')
    with _connect(postgres_dsn, ROTATED_RUNTIME_ROLE) as conn:
        assert check_compatibility(conn, schema=schema).compatible


def test_runtime_ingest_deduplicates_exposes_gaps_and_bounds_reads(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_ingest")
    _migrate_current(postgres_dsn, schema)
    stream_id = str(uuid4())
    first_event = _event(origin_stream_id=stream_id, origin_seq=1, number=20)
    gap_event = _event(origin_stream_id=stream_id, origin_seq=3, number=22)
    second_event = _event(origin_stream_id=stream_id, origin_seq=2, number=21)

    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        first = ingest_observation(conn, first_event, schema=schema)
        retried = ingest_observation(conn, first_event, schema=schema)
        assert retried.receipt_id == first.receipt_id
        assert retried.duplicate
        assert retried.duplicate_count == 1

        changed = _event(
            origin_stream_id=stream_id,
            origin_seq=1,
            number=20,
            summary="Different content",
        )
        with pytest.raises(IngestConflictError, match="different content"):
            ingest_observation(conn, changed, schema=schema)
        with pytest.raises(IngestGapError) as gap:
            ingest_observation(conn, gap_event, schema=schema)
        assert (gap.value.expected, gap.value.received) == (2, 3)
        assert (
            get_stream_status(
                conn, schema=schema, origin_stream_id=stream_id
            ).next_expected_seq
            == 2
        )

        second = ingest_observation(conn, second_event, schema=schema)
        assert second.ingest_offset > first.ingest_offset
        rows = list_observations(conn, schema=schema, after_offset=0, limit=2)
        assert [row["event_id"] for row in rows] == [
            first_event["id"],
            second_event["id"],
        ]
        assert get_receipt(conn, schema=schema, receipt_id=first.receipt_id) is not None
        with pytest.raises(ValueError, match="between 1 and 100"):
            list_observations(conn, schema=schema, limit=101)


def test_concurrent_retry_returns_one_receipt_and_isolated_schemas_do_not_mix(
    postgres_dsn: str,
) -> None:
    schema_a = _schema("audit_dev_a")
    schema_b = _schema("audit_dev_b")
    _migrate_current(postgres_dsn, schema_a)
    _migrate_current(postgres_dsn, schema_b)
    stream_id = str(uuid4())
    event = _event(origin_stream_id=stream_id, origin_seq=1, number=30)
    barrier = threading.Barrier(2)
    receipts = []
    failures: list[BaseException] = []

    def ingest_a() -> None:
        try:
            with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
                barrier.wait()
                receipts.append(ingest_observation(conn, event, schema=schema_a))
        except BaseException as exc:  # pragma: no cover - reported by assertion
            failures.append(exc)

    threads = [threading.Thread(target=ingest_a) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not failures
    assert len({receipt.receipt_id for receipt in receipts}) == 1
    assert sorted(receipt.duplicate for receipt in receipts) == [False, True]

    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        other = ingest_observation(conn, event, schema=schema_b)
        first = get_receipt(conn, schema=schema_a, event_id=event["id"])
    assert first is not None
    assert other.receipt_id != first.receipt_id
    assert other.duplicate_count == 0


def test_concurrent_streams_reusing_global_event_id_get_owner_conflict_without_residue(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_event_race")
    _migrate_current(postgres_dsn, schema)

    for attempt in range(12):
        streams = (str(uuid4()), str(uuid4()))
        events = [
            _event(
                origin_stream_id=stream_id,
                origin_seq=1,
                number=100 + attempt,
                summary=f"Competing stream {index}",
            )
            for index, stream_id in enumerate(streams)
        ]
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        receipts: list[tuple[str, Any]] = []
        conflicts: list[tuple[str, IngestConflictError]] = []
        failures: list[BaseException] = []

        def ingest_competing(stream_id: str, event: dict[str, Any]) -> None:
            try:
                with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
                    barrier.wait()
                    receipt = ingest_observation(conn, event, schema=schema)
                with result_lock:
                    receipts.append((stream_id, receipt))
            except IngestConflictError as exc:
                with result_lock:
                    conflicts.append((stream_id, exc))
            except BaseException as exc:  # pragma: no cover - reported by assertion
                with result_lock:
                    failures.append(exc)

        threads = [
            threading.Thread(target=ingest_competing, args=(stream_id, event))
            for stream_id, event in zip(streams, events, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not failures
        assert not any(thread.is_alive() for thread in threads)
        assert len(receipts) == 1
        assert len(conflicts) == 1
        assert "event_id already identifies" in str(conflicts[0][1])
        winner_stream, receipt = receipts[0]
        loser_stream = conflicts[0][0]
        assert winner_stream != loser_stream

        with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
            persisted = get_receipt(conn, schema=schema, event_id=events[0]["id"])
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT origin_stream_id::text FROM "{schema}".ingest_stream '
                    "WHERE origin_stream_id IN (%s, %s)",
                    streams,
                )
                persisted_streams = {row["origin_stream_id"] for row in cur.fetchall()}
        assert persisted is not None
        assert persisted.receipt_id == receipt.receipt_id
        assert persisted_streams == {winner_stream}


def test_role_contract_denies_runtime_ddl_and_migration_role_serving(
    postgres_dsn: str,
) -> None:
    schema = _schema("audit_roles")
    _migrate_current(postgres_dsn, schema)
    event = _event(origin_stream_id=str(uuid4()), origin_seq=1, number=40)

    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        compatibility = check_compatibility(conn, schema=schema)
        assert compatibility.to_dict() == {
            "schema": schema,
            "domain_api_version": "audit/v1",
            "installed_schema_version": 2,
            "minimum_schema_version": 2,
            "maximum_schema_version": 2,
            "current_role": RUNTIME_ROLE,
            "expected_role_kind": "runtime",
            "configured_role": RUNTIME_ROLE,
            "compatible": True,
            "reasons": [],
        }
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE "{schema}".runtime_ddl_forbidden (id integer)'
                )

    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        compatibility = check_compatibility(conn, schema=schema)
        assert not compatibility.compatible
        assert compatibility.reasons == ("role_kind_mismatch",)
        with pytest.raises(SchemaCompatibilityError, match="role_kind_mismatch"):
            ingest_observation(conn, event, schema=schema)
