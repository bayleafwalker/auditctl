"""The evidence-ledger falsifier, on fixture trees only.

Both shapes are proved here: a tree the falsifier passes and a tree it fails. A falsifier
that only ever fires is indistinguishable from a broken check, and the whole value of this
one is that a failing run against the real tree is a finding rather than a bug.

Every artifacts root and every ledger store below is built under `tmp_path`. Nothing reads
or writes the live `/projects/dev` evidence -- that is the defect agentops `b55df7e` fixed
after two suites had already deposited fixture events into a production ledger. The
`clean_env` autouse fixture in `conftest.py` unsets `AUDITCTL_DB` and
`AUDITCTL_ARTIFACTS_ROOT`, so nothing here can resolve to a real store by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auditctl import db
from auditctl.cli import cli
from auditctl.evidence_falsifier import (
    DECISION_ONLY_IN_WORKING_TREE,
    NO_LEDGER_ID,
    UNRESOLVABLE_LEDGER_ID,
    build_ledger_index,
    discover_evidence_artifacts,
    falsify,
    is_ledger_id,
    render_report,
)
from auditctl.validation import validate_event_object

_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _event_id(n: int) -> str:
    body = "".join(_ID_ALPHABET[(n // (32**i)) % 32] for i in reversed(range(26)))
    return f"ad:{body}"


def _event(n: int, *, record_class: str = "observation") -> dict:
    ts = "2026-09-01T00:00:00Z"
    event = validate_event_object(
        {
            "id": _event_id(n),
            "ts": ts,
            "type": "verification.result" if record_class == "observation" else "decision",
            "actor": "fixture",
            "summary": "fixture evidence record",
            "detail": None,
            "refs": [],
            "source": "manual",
            "metadata": {},
            "created_at": ts,
        }
    )
    if record_class == "decision":
        event["record_class"] = "decision"
    return event


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


def _result(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_the_failing_shape_is_detected_and_named(tmp_path: Path) -> None:
    """An evidence artifact with no ledger id falsifies the settlement."""

    tree = tmp_path / "tree"
    orphan = _result(
        tree / "verification" / "results" / "orphan.json",
        {"schema_version": "verification-result/v1", "context_id": "fixture", "notes": []},
    )

    report = falsify([tree], build_ledger_index())

    assert report.falsified
    assert [offender.path for offender in report.offenders] == [orphan]
    assert report.offenders[0].reason == NO_LEDGER_ID
    rendered = render_report(report)
    assert "FALSIFIED" in rendered
    # The paths must be in the output; a count alone cannot be acted on.
    assert str(orphan) in rendered


def test_the_passing_shape_is_not_falsified(tmp_path: Path) -> None:
    """An artifact citing a resolvable ledger id passes, so the check discriminates."""

    tree = tmp_path / "tree"
    ledger_id = _event_id(1)
    _result(
        tree / "verification" / "results" / "cited.json",
        {
            "schema_version": "verification-result/v1",
            "context_id": "fixture",
            "ledger_id": ledger_id,
        },
    )
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(1)],
    )

    report = falsify([tree], build_ledger_index(shards=[shard]))

    assert not report.falsified
    assert report.offenders == ()
    assert len(report.artifacts) == 1
    assert report.artifacts[0].cited_ids == (ledger_id,)
    assert "NOT FALSIFIED" in render_report(report)


def test_a_well_formed_id_that_names_no_event_is_reported(tmp_path: Path) -> None:
    """Citing an unresolvable id is worse than citing none: it reads as compliant."""

    tree = tmp_path / "tree"
    _result(
        tree / "verification" / "results" / "dangling.json",
        {"schema_version": "verification-result/v1", "ledger_id": _event_id(999)},
    )
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(1)],
    )

    report = falsify([tree], build_ledger_index(shards=[shard]))

    assert report.falsified
    assert report.offenders[0].reason == UNRESOLVABLE_LEDGER_ID
    assert report.offenders[0].cited_ids == (_event_id(999),)


def test_a_judgement_with_only_an_observation_behind_it_trips_the_second_limb(
    tmp_path: Path,
) -> None:
    """The falsifier's other half: a decision reachable only by reading this file."""

    tree = tmp_path / "tree"
    observation_id = _event_id(2)
    _result(
        tree / "campaigns" / "2026-09-01" / "case" / "evaluation.json",
        {
            "schema_version": "campaign-evaluation/v1",
            "status": "PASS",
            "aggregate_score": 1.0,
            "ledger_id": observation_id,
        },
    )
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(2)],
    )

    report = falsify([tree], build_ledger_index(shards=[shard]))

    assert report.falsified
    assert report.offenders[0].reason == DECISION_ONLY_IN_WORKING_TREE
    assert report.offenders[0].cited_ids == (observation_id,)


def test_a_judgement_citing_a_ledger_decision_passes(tmp_path: Path) -> None:
    """Recording the decision in the ledger is the actual remedy, so it must pass."""

    tree = tmp_path / "tree"
    decision_id = _event_id(3)
    _result(
        tree / "campaigns" / "2026-09-01" / "case" / "evaluation.json",
        {
            "schema_version": "campaign-evaluation/v1",
            "status": "PASS",
            "aggregate_score": 1.0,
            "ledger_id": decision_id,
        },
    )
    index = _write_index(
        tmp_path / "repo" / ".auditctl" / "auditctl.db", [_event(3, record_class="decision")]
    )

    ledger = build_ledger_index(indexes=[index])
    assert ledger.holds_decision(decision_id)

    report = falsify([tree], ledger)
    assert not report.falsified


def test_a_citation_in_prose_counts_as_a_citation(tmp_path: Path) -> None:
    """Liberal about where the id sits, strict about whether it resolves.

    No field name for the citation has been agreed yet, so requiring one would report
    every artifact as an offender for a reason the settlement does not state.
    """

    tree = tmp_path / "tree"
    ledger_id = _event_id(4)
    note = tree / "campaigns" / "2026-09-02" / "README.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"Commissioned by ledger record {ledger_id}.\n", encoding="utf-8")
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(4)],
    )

    report = falsify([tree], build_ledger_index(shards=[shard]))

    assert not report.falsified
    assert report.artifacts[0].cited_ids == (ledger_id,)


def test_non_evidence_files_are_not_examined(tmp_path: Path) -> None:
    """Only the declared shapes. Classing every file as evidence makes the check noise."""

    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tree / "README.md").write_text("a readme, not evidence\n", encoding="utf-8")
    _result(
        tree / "verification" / "contexts" / "fixture.json",
        {"schema_version": "test-context/v1"},
    )

    artifacts = discover_evidence_artifacts([tree])
    assert artifacts == []
    assert not falsify([tree], build_ledger_index()).falsified


def test_classification_does_not_depend_on_where_the_scan_root_is_placed(
    tmp_path: Path,
) -> None:
    """Scanning the campaigns directory itself must find what scanning its parent finds.

    Matching the path *below* the root consumed the very directory that names these
    artifacts, so pointing at `.../campaigns` classified none of them. The campaign files
    in the real tree declare `schema_version: 1`, which names no schema, so the directory
    shape is the only signal there is.
    """

    lab = tmp_path / "acceptance-lab"
    evaluation = _result(
        lab / "campaigns" / "2026-09-01" / "case" / "evaluation.json",
        {"schema_version": 1, "status": "PASS"},
    )

    from_parent = falsify([lab], build_ledger_index())
    from_campaigns = falsify([lab / "campaigns"], build_ledger_index())

    assert [a.path for a in from_parent.artifacts] == [evaluation]
    assert [a.path for a in from_campaigns.artifacts] == [evaluation]
    assert from_parent.falsified and from_campaigns.falsified


def test_a_declared_evidence_schema_is_examined_wherever_it_sits(tmp_path: Path) -> None:
    """Directory names are a convention; the declared schema is a claim."""

    tree = tmp_path / "tree"
    stray = _result(
        tree / "somewhere" / "else" / "result.json",
        {"schema_version": "verification-result/v1", "context_id": "fixture"},
    )

    report = falsify([tree], build_ledger_index())

    assert [offender.path for offender in report.offenders] == [stray]


def test_is_ledger_id_accepts_only_a_whole_event_id() -> None:
    assert is_ledger_id(_event_id(5))
    assert not is_ledger_id("ad:not-an-id")
    assert not is_ledger_id(f"prefix {_event_id(5)}")


def test_cli_exits_non_zero_when_falsified_and_lists_the_paths(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    orphan = _result(
        tree / "verification" / "results" / "orphan.json",
        {"schema_version": "verification-result/v1"},
    )
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(6)],
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["check", "evidence-ledger", "--root", str(tree), "--shards", str(shard)],
    )
    assert result.exit_code == 1, result.output
    assert str(orphan) in result.output

    reported = runner.invoke(
        cli,
        [
            "check",
            "evidence-ledger",
            "--root",
            str(tree),
            "--shards",
            str(shard),
            "--report-only",
        ],
    )
    assert reported.exit_code == 0, reported.output
    assert "FALSIFIED" in reported.output

    as_json = runner.invoke(
        cli,
        [
            "check",
            "evidence-ledger",
            "--root",
            str(tree),
            "--shards",
            str(shard),
            "--json",
            "--report-only",
        ],
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["falsified"] is True
    assert payload["artifacts_examined"] == 1
    assert payload["offenders_by_reason"][NO_LEDGER_ID] == 1
    assert payload["offenders"][0]["path"] == str(orphan)
    # The scan stayed inside the fixture tree.
    assert all(path.startswith(str(tmp_path)) for path in payload["roots"])
    assert all(path.startswith(str(tmp_path)) for path in payload["ledger"]["stores"])


def test_cli_exits_zero_on_a_tree_that_cites_its_ledger(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _result(
        tree / "verification" / "results" / "cited.json",
        {"schema_version": "verification-result/v1", "ledger_id": _event_id(7)},
    )
    shard = _write_shard(
        tmp_path / "_artifacts" / "fixture" / "audit" / "events-2026-09-01.ndjson",
        [_event(7)],
    )

    result = CliRunner().invoke(
        cli, ["check", "evidence-ledger", "--root", str(tree), "--shards", str(shard)]
    )

    assert result.exit_code == 0, result.output
    assert "NOT FALSIFIED" in result.output
