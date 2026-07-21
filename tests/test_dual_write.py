from __future__ import annotations

import json
import multiprocessing
import sqlite3
import traceback
from pathlib import Path
from queue import Empty
from typing import Any

from click.testing import CliRunner

from auditctl.cli import cli
from auditctl import db, ndjson
from auditctl.validation import validate_event_object, with_observation_envelope


def _invoke_add_histories(
    root: str,
    index: int,
    history_count: int,
    start_barrier: Any,
    results: Any,
) -> None:
    import os

    for history in range(history_count):
        try:
            history_root = Path(root) / f"history-{history}"
            repo = history_root / "example-repo"
            artifacts = history_root / "artifacts"
            os.chdir(repo)
            os.environ["AUDITCTL_DB"] = str(repo / ".auditctl" / "auditctl.db")
            os.environ["AUDITCTL_ARTIFACTS_ROOT"] = str(artifacts)
            start_barrier.wait(timeout=10)
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
                exception = (
                    "".join(traceback.format_exception(result.exception))
                    if result.exception is not None
                    else "no captured exception"
                )
                raise RuntimeError(
                    f"auditctl add exited {result.exit_code}\n"
                    f"output:\n{result.output}\nexception:\n{exception}"
                )
        except BaseException:
            results.put((history, index, traceback.format_exc()))
            start_barrier.abort()
            raise
        else:
            results.put((history, index, None))


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
        assert conn.execute("SELECT count(*) FROM producer_stream").fetchone()[0] == 0
    finally:
        conn.close()


def test_concurrent_writers_produce_complete_lines(tmp_path: Path) -> None:
    history_count = 16
    process_context = multiprocessing.get_context("fork")
    for history in range(history_count):
        history_root = tmp_path / f"history-{history}"
        repo_root = history_root / "example-repo"
        repo_root.mkdir(parents=True)
        (repo_root / ".git").mkdir()

    start_barrier = process_context.Barrier(8)
    results = process_context.Queue()
    processes = [
        process_context.Process(
            target=_invoke_add_histories,
            args=(str(tmp_path), index, history_count, start_barrier, results),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()

    child_results = []
    for _ in range(history_count * 8):
        try:
            child_results.append(results.get(timeout=5))
        except Empty:
            break
    diagnostics = "\n".join(
        f"history {history}, worker {index}:\n{diagnostic}"
        for history, index, diagnostic in sorted(child_results)
        if diagnostic is not None
    )
    exitcodes = [process.exitcode for process in processes]
    assert len(child_results) == history_count * 8, (
        f"only {len(child_results)}/{history_count * 8} child histories reported; "
        f"exitcodes={exitcodes}\n{diagnostics}"
    )
    assert exitcodes == [0] * 8, f"child exitcodes={exitcodes}\n{diagnostics}"

    for history in range(history_count):
        artifacts_root = tmp_path / f"history-{history}" / "artifacts"
        shard = (
            artifacts_root
            / "_artifacts"
            / "example-repo"
            / "audit"
            / "events-2026-04-26.ndjson"
        )
        lines = shard.read_text().splitlines()
        assert len(lines) == 8
        events = [json.loads(line) for line in lines]
        assert all(event["type"] == "commit" for event in events)
        assert len({event["origin_stream_id"] for event in events}) == 1
        assert sorted(event["origin_seq"] for event in events) == list(range(1, 9))


def test_restart_reconciles_fsynced_ndjson_before_reusing_sequence(
    repo_root: Path, tmp_path: Path
) -> None:
    db_path = repo_root / ".auditctl" / "auditctl.db"
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    origin_stream_id, origin_seq = db.allocate_origin(conn)
    ahead = validate_event_object(
        with_observation_envelope(
            {
                "id": "ad:01HWXYZ0000000000000000000",
                "ts": "2026-04-26T10:00:00Z",
                "type": "commit",
                "actor": "crashed-writer",
                "summary": "fsynced before crash",
                "detail": None,
                "refs": ["sha:ahead"],
                "source": "test",
                "metadata": {},
                "created_at": "2026-04-26T10:00:01Z",
            },
            origin_stream_id=origin_stream_id,
            origin_seq=origin_seq,
        )
    )
    db.insert_event(conn, ahead)
    ndjson.append_event(shard, ahead)
    conn.rollback()
    conn.close()

    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "commit",
            "--actor",
            "restarted-writer",
            "--summary",
            "after restart",
            "--ts",
            "2026-04-26T10:00:02Z",
        ],
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in shard.read_text().splitlines()]
    assert [event["origin_seq"] for event in events] == [1, 2]
    assert {event["origin_stream_id"] for event in events} == {origin_stream_id}
    rebuilt = db.connect(db_path)
    try:
        assert len(db.query_events(rebuilt, limit=None)) == 2
    finally:
        rebuilt.close()
