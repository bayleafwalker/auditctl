"""Decode failures keep rebuild's existing typed, read-only rejection boundary."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from click.testing import CliRunner

from auditctl import db
from auditctl.cli import cli
from auditctl.ndjson import ImportInputError, read_events
from auditctl.validation import with_observation_envelope


def _event(sequence=1, *, envelope=True):
    value = dict(id="ad:" + str(sequence).zfill(26), ts="2026-04-26T10:00:00Z",
                 created_at="2026-04-26T10:00:01Z", type="decision", actor="fixture",
                 summary="Keep café 東京", detail=None, refs=[], source="fixture", metadata={})
    if envelope:
        value = with_observation_envelope(value,
            origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20", origin_seq=sequence)
    return value


def _encoded(events):
    return b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events)


def _sources(root, shape, events):
    directory = root / "inputs"
    directory.mkdir()
    valid = _encoded(events)
    first = directory / "events-2026-04-26.ndjson"
    if shape == "invalid-prefix":
        first.write_bytes(b"\xff" + valid)
    elif shape == "invalid-tail":
        # Past TextIO's initial decode buffer: a real valid record can already
        # have been yielded when the subsequent invalid bytes are reached.
        first.write_bytes(valid + b"\n" * 9000 + b"\xff")
    elif shape == "second-shard":
        first.write_bytes(valid)
        (directory / "events-2026-04-27.ndjson").write_bytes(b"\xe2\x82")
    else:
        assert shape == "deep-json"
        first.write_bytes(valid + b"[" * 20000 + b"0" + b"]" * 20000 + b"\n")
    return sorted(directory.glob("events-*.ndjson"))


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _durable_rows(index):
    # The fixture closed its only writer, so the seed is checkpointed. This
    # independent immutable connection does not create WAL/SHM bookkeeping.
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize("shape", ["invalid-prefix", "invalid-tail", "second-shard", "deep-json"])
def test_reader_decode_failure_uses_corrupt_shard(tmp_path, shape):
    event = _event()
    paths = _sources(tmp_path, shape, [event])
    records = read_events(paths)
    if shape != "invalid-prefix":
        assert next(records) == event
    with pytest.raises(ImportInputError, match="^corrupt_shard$") as rejection:
        list(records)
    assert rejection.value.code == "corrupt_shard"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--replace"]], ids=["ordinary", "dry-run", "replace"])
@pytest.mark.parametrize("shape", ["invalid-prefix", "invalid-tail", "second-shard", "deep-json"])
def test_rebuild_decode_rejection_preserves_sources_destination_and_cursor(repo_root, tmp_path, shape, mode, existing):
    index = repo_root / ".auditctl" / "auditctl.db"
    events = [_event()]
    if existing:
        with closing(db.connect(index)) as conn:
            db.init_db(conn)
            db.import_events(conn, events)
        events.append(_event(2))
    paths = _sources(tmp_path, shape, events)
    before_files = _files(tmp_path)
    before_rows = _durable_rows(index) if existing else None
    result = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(paths[0].parent), *mode])
    assert result.exit_code == 1
    assert result.output == "Error: rebuild rejected [corrupt_shard]\n"
    assert _files(tmp_path) == before_files
    if existing:
        assert _durable_rows(index) == before_rows
    else:
        assert not index.parent.exists()


@pytest.mark.parametrize("shape", ["invalid-tail", "deep-json"])
def test_real_process_rejects_decode_failure_before_replacement(repo_root, tmp_path, shape):
    index = repo_root / ".auditctl" / "auditctl.db"
    with closing(db.connect(index)) as conn:
        db.init_db(conn)
        db.import_events(conn, [_event()])
    paths = _sources(tmp_path, shape, [_event(), _event(2)])
    before = _files(tmp_path)
    result = subprocess.run([sys.executable, "-m", "auditctl.cli", "rebuild",
        "--from-ndjson", str(paths[0].parent), "--replace"],
        cwd=repo_root, env=os.environ.copy(), capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Error: rebuild rejected [corrupt_shard]\n"
    assert _files(tmp_path) == before


@pytest.mark.parametrize("envelope", [False, True])
def test_valid_historical_large_unicode_record_still_rebuilds_idempotently(repo_root, tmp_path, envelope):
    event = _event(envelope=False)
    event["detail"] = "東京" * 7000
    if envelope:
        event = with_observation_envelope(event,
            origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20", origin_seq=1)
    shard = tmp_path / "historical.ndjson"
    shard.write_bytes(_encoded([event]))
    before = shard.read_bytes()
    runner = CliRunner()
    first = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard)])
    assert first.exit_code == 0, first.output
    repeated = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard)])
    assert repeated.exit_code == 0, repeated.output
    assert "0 imported, 1 skipped" in repeated.output
    assert shard.read_bytes() == before
    with closing(db.connect(repo_root / ".auditctl" / "auditctl.db")) as conn:
        assert db.query_events(conn) == [event]
