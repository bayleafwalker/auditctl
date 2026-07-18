from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from auditctl.cli import cli


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
