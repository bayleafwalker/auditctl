from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from auditctl import db
from auditctl.cli import cli
from auditctl.validation import validate_event_object, with_observation_envelope


def test_rebuild_from_directory_round_trips(repo_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    add = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "decision",
            "--actor",
            "tester",
            "--summary",
            "Keep shards",
            "--ts",
            "2026-04-26T10:00:00Z",
        ],
    )
    assert add.exit_code == 0, add.output
    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"

    (repo_root / ".auditctl" / "auditctl.db").unlink()
    rebuild = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--replace"])
    assert rebuild.exit_code == 0, rebuild.output
    assert "1 imported" in rebuild.output

    listed = runner.invoke(cli, ["list", "--json"])
    assert listed.exit_code == 0, listed.output
    rebuilt_event = json.loads(listed.output)[0]
    assert rebuilt_event["summary"] == "Keep shards"
    assert rebuilt_event["origin_seq"] == 1

    next_add = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "decision",
            "--actor",
            "tester",
            "--summary",
            "Continue stream",
            "--ts",
            "2026-04-26T10:00:02Z",
        ],
    )
    assert next_add.exit_code == 0, next_add.output
    events = [json.loads(line) for line in next(shard_dir.glob("events-*.ndjson")).read_text().splitlines()]
    assert [event["origin_seq"] for event in events] == [1, 2]
    assert len({event["origin_stream_id"] for event in events}) == 1


def test_rebuild_duplicate_lines_are_ignored(repo_root: Path, tmp_path: Path) -> None:
    shard = tmp_path / "events-2026-04-26.ndjson"
    event = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "tester",
        "summary": "Duplicate",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {},
        "created_at": "2026-04-26T10:00:01Z",
    }
    shard.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n")
    result = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard)])
    assert result.exit_code == 0, result.output
    assert "1 imported, 1 skipped" in result.output


def test_rebuild_dry_run_does_not_write(repo_root: Path, tmp_path: Path) -> None:
    shard = tmp_path / "events-2026-04-26.ndjson"
    event = {
        "id": "ad:01HWXYZ0000000000000000000",
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "tester",
        "summary": "Dry",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {},
        "created_at": "2026-04-26T10:00:01Z",
    }
    shard.write_text(json.dumps(event) + "\n")
    result = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Validated 1 shard" in result.output
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()


def _base_event(*, event_id: str = "ad:01HWXYZ0000000000000000000") -> dict[str, object]:
    return {
        "id": event_id,
        "ts": "2026-04-26T10:00:00Z",
        "type": "decision",
        "actor": "tester",
        "summary": "Imported record",
        "detail": None,
        "refs": [],
        "source": "test",
        "metadata": {},
        "created_at": "2026-04-26T10:00:01Z",
    }


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "case",
    [
        "malformed_envelope",
        "unsupported_schema",
        "unsupported_class",
        "incompatible_duplicate",
        "origin_discontinuity",
        "corrupt_shard",
        "missing_shard",
    ],
)
def test_rebuild_rejects_whole_batch_without_mutating_ledger_or_sources(
    repo_root: Path, tmp_path: Path, case: str
) -> None:
    runner = CliRunner()
    seeded = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "decision",
            "--actor",
            "tester",
            "--summary",
            "Seed ledger",
            "--ts",
            "2026-04-26T09:00:00Z",
        ],
    )
    assert seeded.exit_code == 0, seeded.output
    db_path = repo_root / ".auditctl" / "auditctl.db"
    audit_root = tmp_path / "_artifacts" / "example-repo" / "audit"
    source = tmp_path / "events-2026-04-26.ndjson"
    persisted = json.loads(next(audit_root.glob("events-*.ndjson")).read_text().splitlines()[0])

    if case == "corrupt_shard":
        source.write_text('{"id":')
    elif case == "missing_shard":
        source = tmp_path / "missing.ndjson"
    else:
        candidate = _base_event(event_id="ad:01HWXYZ0000000000000000001")
        if case == "malformed_envelope":
            candidate["schema_version"] = 1
        elif case == "unsupported_schema":
            candidate = with_observation_envelope(
                candidate,
                origin_stream_id=persisted["origin_stream_id"],
                origin_seq=2,
            )
            candidate["schema_version"] = 99
        elif case == "unsupported_class":
            candidate = with_observation_envelope(
                candidate,
                origin_stream_id=persisted["origin_stream_id"],
                origin_seq=2,
            )
            candidate["record_class"] = "unknown"
        elif case == "incompatible_duplicate":
            conflicting = _base_event(event_id=candidate["id"])
            conflicting["summary"] = "Same ID, different payload"
            source.write_text(json.dumps(candidate) + "\n" + json.dumps(conflicting) + "\n")
            candidate = None
        elif case == "origin_discontinuity":
            candidate = with_observation_envelope(
                candidate,
                origin_stream_id=persisted["origin_stream_id"],
                origin_seq=3,
            )
        if candidate is not None:
            source.write_text(json.dumps(candidate) + "\n")

    before_db = db_path.read_bytes()
    before_sources = _tree_bytes(tmp_path)
    result = runner.invoke(cli, ["rebuild", "--from-ndjson", str(source), "--replace"])

    assert result.exit_code != 0
    assert db_path.read_bytes() == before_db
    assert _tree_bytes(tmp_path) == before_sources
    conn = db.connect(db_path)
    try:
        assert len(db.query_events(conn, limit=None)) == 1
        stream = conn.execute(
            "SELECT origin_stream_id, next_origin_seq FROM producer_stream WHERE singleton = 1"
        ).fetchone()
        assert stream["origin_stream_id"] == persisted["origin_stream_id"]
        assert stream["next_origin_seq"] == 2
    finally:
        conn.close()
