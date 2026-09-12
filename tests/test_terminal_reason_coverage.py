"""The terminal-reason coverage reporter, measured against fixture stores only.

Every store in this module is built under `tmp_path`. Nothing here reads or writes the
live `/projects/dev` evidence: a test that resolves to the real index is the defect
agentops `b55df7e` fixed after the suites had already put ten fixture events into a
production ledger. The `clean_env` autouse fixture in `conftest.py` unsets `AUDITCTL_DB`
and `AUDITCTL_ARTIFACTS_ROOT`, and the assertions below never pass a path that is not a
child of `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auditctl import db
from auditctl.cli import cli
from auditctl.coverage import (
    discover_stores,
    measure_terminal_reasons,
    render_terminal_reason_coverage,
)
from auditctl.validation import TERMINAL_REASON_CODES, validate_event_object

_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _event_id(n: int) -> str:
    """A well-formed Crockford-base32 event id, distinct per n."""
    body = "".join(_ID_ALPHABET[(n // (32**i)) % 32] for i in reversed(range(26)))
    return f"ad:{body}"


def _dispatch_exit(n: int, terminal_reason: str, *, ts: str = "2026-09-01T00:00:00Z") -> dict:
    return validate_event_object(
        {
            "id": _event_id(n),
            "ts": ts,
            "type": "dispatch.exit",
            "actor": "claude-hook",
            "summary": f"dispatch exit {terminal_reason}",
            "detail": None,
            "refs": [],
            "source": "claude-hook",
            "metadata": {
                "terminal_reason": terminal_reason,
                "session": f"sess-{n}",
                "agent_id": "fixture-agent",
                "project": "fixture-project",
            },
            "created_at": ts,
        }
    )


def _write_shard(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    return path


def _write_index(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    try:
        db.init_db(conn)
        for event in events:
            db.insert_event_ignore(conn, event)
        conn.commit()
    finally:
        conn.close()
    return path


def test_reporter_identifies_an_unwritten_code(tmp_path: Path) -> None:
    """The whole point: a declared code with no observation is named, not scored away."""

    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [
            _dispatch_exit(1, "completed"),
            _dispatch_exit(2, "completed"),
            _dispatch_exit(3, "cancelled"),
        ],
    )

    coverage = measure_terminal_reasons(shards=[shard])

    counts = {row.code: row.count for row in coverage.rows}
    assert counts["completed"] == 2
    assert counts["cancelled"] == 1
    assert counts["usage-limit"] == 0
    assert "usage-limit" in coverage.unwritten
    assert "completed" not in coverage.unwritten
    # Every declared code appears in the report, written or not. A reporter that only
    # listed the codes it found could never surface the ones that matter.
    assert {row.code for row in coverage.rows} == set(TERMINAL_REASON_CODES)


def test_reporter_reports_full_coverage_when_every_code_was_written(tmp_path: Path) -> None:
    """The passing shape, so an always-failing check cannot masquerade as a finding."""

    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-02.ndjson",
        [
            _dispatch_exit(index, code, ts="2026-09-02T00:00:00Z")
            for index, code in enumerate(sorted(TERMINAL_REASON_CODES), start=10)
        ],
    )

    coverage = measure_terminal_reasons(shards=[shard])

    assert coverage.unwritten == ()
    assert set(coverage.written) == set(TERMINAL_REASON_CODES)
    assert all(row.count == 1 for row in coverage.rows)
    assert "Every declared terminal reason" in render_terminal_reason_coverage(coverage)


def test_an_event_in_both_index_and_shard_is_counted_once(tmp_path: Path) -> None:
    """`add` writes both stores, so a union count would double every live event."""

    events = [_dispatch_exit(20, "usage-limit"), _dispatch_exit(21, "timeout")]
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-03.ndjson", events
    )
    index = _write_index(tmp_path / "repo" / ".auditctl" / "auditctl.db", events)

    coverage = measure_terminal_reasons(indexes=[index], shards=[shard])

    counts = {row.code: row.count for row in coverage.rows}
    assert counts["usage-limit"] == 1
    assert counts["timeout"] == 1
    assert coverage.bearing_events == 2
    # Both stores really were read; the count is deduplicated, not half-scanned.
    assert coverage.events_scanned == 4


def test_a_code_written_only_to_the_index_still_counts(tmp_path: Path) -> None:
    """Index-only events are exactly the ones a shard-only scan would miss."""

    index = _write_index(
        tmp_path / "repo" / ".auditctl" / "auditctl.db",
        [_dispatch_exit(30, "start-failed", ts="2026-08-15T00:00:00Z")],
    )

    coverage = measure_terminal_reasons(indexes=[index])

    assert "start-failed" not in coverage.unwritten
    assert coverage.first_ts == "2026-08-15T00:00:00Z"


def test_a_historical_event_todays_validator_would_reject_is_still_counted(
    tmp_path: Path,
) -> None:
    """Lenient on purpose: strict reading would drop the rarest codes with the shard."""

    shard = tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-07-01.ndjson"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(
        json.dumps({"id": "legacy-1", "ts": "2026-07-01T00:00:00Z", "metadata": {}})
        + "\n"
        + json.dumps(
            {
                "id": "legacy-2",
                "ts": "2026-07-01T01:00:00Z",
                "metadata": {"terminal_reason": "process-exit"},
            }
        )
        + "\n"
        + "{ not json at all\n",
        encoding="utf-8",
    )

    coverage = measure_terminal_reasons(shards=[shard])

    assert "process-exit" not in coverage.unwritten
    assert coverage.bearing_events == 1
    assert len(coverage.unreadable) == 1
    assert "unparseable line" in coverage.unreadable[0]


def test_off_vocabulary_values_are_reported_separately(tmp_path: Path) -> None:
    """An unlisted code in evidence is a contract breach, not a coverage credit."""

    shard = tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-07-02.ndjson"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(
        json.dumps(
            {
                "id": "legacy-3",
                "ts": "2026-07-02T00:00:00Z",
                "metadata": {"terminal_reason": "ran-out-of-vibes"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    coverage = measure_terminal_reasons(shards=[shard])

    assert coverage.off_vocabulary == {"ran-out-of-vibes": 1}
    assert set(coverage.unwritten) == set(TERMINAL_REASON_CODES)


def test_discover_stores_finds_pooled_shards_and_per_repo_indexes(tmp_path: Path) -> None:
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-04.ndjson",
        [_dispatch_exit(40, "completed")],
    )
    index = _write_index(tmp_path / "some-repo" / ".auditctl" / "auditctl.db", [])

    indexes, shards = discover_stores([tmp_path])

    assert index in indexes
    assert shard in shards


def test_cli_lists_every_code_and_exits_non_zero_under_strict(tmp_path: Path) -> None:
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-05.ndjson",
        [_dispatch_exit(50, "completed")],
    )
    runner = CliRunner()

    result = runner.invoke(
        cli, ["coverage", "terminal-reason", "--shards", str(shard)]
    )
    assert result.exit_code == 0, result.output
    for code in TERMINAL_REASON_CODES:
        assert code in result.output
    assert "NEVER WRITTEN" in result.output
    assert "usage-limit" in result.output

    strict = runner.invoke(
        cli, ["coverage", "terminal-reason", "--shards", str(shard), "--strict"]
    )
    assert strict.exit_code == 1, strict.output

    as_json = runner.invoke(
        cli, ["coverage", "terminal-reason", "--root", str(tmp_path), "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["vocabulary_size"] == len(TERMINAL_REASON_CODES)
    assert "usage-limit" in payload["unwritten"]
    assert payload["observed_range"]["first_ts"] == "2026-09-01T00:00:00Z"
    # The scan stayed inside the fixture tree.
    assert all(path.startswith(str(tmp_path)) for path in payload["shards_scanned"])
    assert all(path.startswith(str(tmp_path)) for path in payload["indexes_scanned"])
