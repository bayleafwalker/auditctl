from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from auditctl import db
from auditctl.central import prepare_observation
from auditctl.cli import cli
from auditctl.validation import validate_event_object, with_observation_envelope


def _event(*, envelope: bool = False, context: bool = True) -> dict:
    event = dict(
        id="ad:01HWXYZ0000000000000000000",
        ts="2026-04-26T10:00:00Z", created_at="2026-04-26T10:00:01Z",
        type="decision", actor="tester", summary="Preserve provenance",
        detail=None, refs=[], source="test", metadata={},
    )
    if context:
        event["resolved_context"] = dict(
            repo_id="example-repo", repo_root="/temporary/repo",
            artifacts_root="/temporary/repo", published_from="/temporary/repo",
            resolution_source="repository", stream_class="live",
        )
    if envelope:
        event = with_observation_envelope(
            event, origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
            origin_seq=1,
        )
    return validate_event_object(event)


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("field", [
    "repo_id", "repo_root", "artifacts_root", "published_from",
    "resolution_source", "stream_class", "absent-context", "absent-stream-class",
])
def test_import_rejects_changed_provenance_without_mutation(envelope, existing, field):
    original = _event(envelope=envelope)
    changed = deepcopy(original)
    if field == "absent-context":
        del changed["resolved_context"]
    elif field == "absent-stream-class":
        del changed["resolved_context"]["stream_class"]
    else:
        changed["resolved_context"][field] = "fixture" if field == "stream_class" else "different"
    validate_event_object(changed)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        if existing:
            db.import_events(conn, [original])
        before = list(conn.iterdump())
        with pytest.raises(db.ImportValidationError, match="incompatible_duplicate_identity"):
            db.import_events(conn, [changed] if existing else [original, changed])
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("context", [False, True])
def test_exact_provenance_retry_and_legacy_absence_remain_idempotent(envelope, context):
    event = _event(envelope=envelope, context=context)
    if context:
        # Historical five-field contexts remain valid without stream_class.
        del event["resolved_context"]["stream_class"]
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        assert db.import_events(conn, [event, deepcopy(event)]) == (1, 1)
        assert db.import_events(conn, [deepcopy(event)]) == (0, 1)
        assert db.query_events(conn)[0] == event


@pytest.mark.parametrize("mode, existing_only", [
    ([], False), (["--dry-run"], False), (["--replace"], False),
    ([], True), (["--dry-run"], True),
])
def test_rebuild_rejects_provenance_collision_before_writing(
    repo_root: Path, tmp_path: Path, mode, existing_only,
):
    index = repo_root / ".auditctl" / "auditctl.db"
    original = _event(envelope=True)
    conn = db.connect(index)
    db.init_db(conn)
    db.import_events(conn, [original])
    conn.close()
    changed = deepcopy(original)
    changed["resolved_context"]["stream_class"] = "fixture"
    source = tmp_path / "collision.ndjson"
    # Both copies are necessary for --replace's empty-ledger preflight.
    records = [changed] if existing_only else [original, changed]
    source.write_text("".join(json.dumps(event) + "\n" for event in records))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(source), *mode])
    assert result.exit_code != 0
    assert "incompatible_duplicate_identity" in result.output
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_local_provenance_does_not_change_central_digest_domain():
    original = _event(envelope=True)
    changed = deepcopy(original)
    changed["resolved_context"]["stream_class"] = "fixture"
    assert prepare_observation(original) == prepare_observation(changed)
