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
    "case, expected_code",
    [
        ("malformed_envelope", "malformed_envelope"),
        ("unsupported_schema", "unsupported_schema"),
        ("unsupported_class", "unsupported_record_class"),
        ("incompatible_duplicate", "incompatible_duplicate_identity"),
        ("origin_discontinuity", "origin_discontinuity"),
        ("corrupt_shard", "corrupt_shard"),
        ("missing_shard", "missing_shard"),
    ],
)
def test_rebuild_rejects_whole_batch_without_mutating_ledger_or_sources(
    repo_root: Path, tmp_path: Path, case: str, expected_code: str
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
    assert f"rebuild rejected [{expected_code}]" in result.output
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


def test_rebuild_refuses_to_drop_events_the_shards_do_not_carry(
    repo_root: Path, tmp_path: Path
) -> None:
    """Shards are authoritative, so a rebuild that would lose rows must stop.

    Measured on 2026-08-28 in the agentops ledger: the index held 493 events
    and its shards 468.  The 25-event difference survived a ledger retirement
    that carried rows into the new index without their shard lines, and
    ``rebuild --dry-run`` still reported "Validated 7 shard(s): 468 event(s)"
    -- a green gate over silent data loss.  The batch validation cannot see
    this: it only inspects the ids the batch itself names.
    """

    runner = CliRunner()
    for index, summary in enumerate(("Kept", "Lost"), start=1):
        added = runner.invoke(
            cli,
            [
                "add",
                "--type", "decision",
                "--actor", "tester",
                "--summary", summary,
                "--ts", f"2026-04-26T10:00:0{index}Z",
            ],
        )
        assert added.exit_code == 0, added.output

    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"
    shard = next(shard_dir.glob("events-*.ndjson"))
    lines = shard.read_text().splitlines()
    assert len(lines) == 2
    # Drop the second event from the shard, keeping it in the index: exactly
    # the shape a lost or unwritten shard leaves behind.
    shard.write_text(lines[0] + "\n")

    rejected = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--dry-run"])
    assert rejected.exit_code != 0
    assert "index_only_events" in rejected.output
    assert "1 event(s) that no shard carries" in rejected.output

    accepted = runner.invoke(
        cli,
        ["rebuild", "--from-ndjson", str(shard_dir), "--dry-run", "--allow-index-only"],
    )
    assert accepted.exit_code == 0, accepted.output
    assert "1 index-only event(s) will be lost" in accepted.output


def test_rebuild_reports_full_coverage_without_the_override(
    repo_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    added = runner.invoke(
        cli,
        [
            "add",
            "--type", "decision",
            "--actor", "tester",
            "--summary", "Covered",
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert added.exit_code == 0, added.output
    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"

    result = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Validated 1 shard(s): 1 event(s)."
