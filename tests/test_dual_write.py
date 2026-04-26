from __future__ import annotations

import json
import sqlite3
from multiprocessing import Process
from pathlib import Path

from click.testing import CliRunner

from auditctl.cli import cli


def _invoke_add(repo: str, artifacts: str, index: int) -> None:
    import os

    os.chdir(repo)
    os.environ["AUDITCTL_DB"] = str(Path(repo) / ".auditctl" / "auditctl.db")
    os.environ["AUDITCTL_ARTIFACTS_ROOT"] = artifacts
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "commit",
            "--actor",
            "worker",
            "--summary",
            f"event {index}",
            "--ref",
            f"sha:{index}",
            "--ts",
            "2026-04-26T10:00:00Z",
        ],
    )
    if result.exit_code != 0:
        raise SystemExit(result.output)


def test_successful_add_writes_sqlite_and_ndjson(repo_root: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "commit",
            "--actor",
            "tester",
            "--summary",
            "Commit abc",
            "--ref",
            "sha:abc",
            "--ts",
            "2026-04-26T10:00:00Z",
        ],
    )
    assert result.exit_code == 0, result.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    lines = shard.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["summary"] == "Commit abc"

    conn = sqlite3.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 1
    finally:
        conn.close()


def test_ndjson_failure_rolls_back_sqlite(repo_root: Path, monkeypatch) -> None:
    def fail_append(path, event):
        raise OSError("disk full")

    monkeypatch.setattr("auditctl.cli.append_event", fail_append)
    result = CliRunner().invoke(
        cli,
        ["add", "--type", "commit", "--actor", "tester", "--summary", "nope"],
    )
    assert result.exit_code != 0
    assert "disk full" in result.output
    conn = sqlite3.connect(repo_root / ".auditctl" / "auditctl.db")
    try:
        assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 0
    finally:
        conn.close()


def test_concurrent_writers_produce_complete_lines(repo_root: Path, tmp_path: Path) -> None:
    processes = [
        Process(target=_invoke_add, args=(str(repo_root), str(tmp_path), index))
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
        assert process.exitcode == 0

    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    lines = shard.read_text().splitlines()
    assert len(lines) == 8
    assert all(json.loads(line)["type"] == "commit" for line in lines)

