from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from auditctl.cli import cli


def test_help_is_grouped_and_versioned() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli, ["--help"])
    assert help_result.exit_code == 0
    assert "Start here:" in help_result.output
    assert "Inspect:" in help_result.output
    assert "Examples:" in help_result.output
    version_result = runner.invoke(cli, ["--version"])
    assert version_result.exit_code == 0
    assert "0.2.0" in version_result.output


def test_add_list_and_render_json(repo_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "commit",
            "--actor",
            "tester",
            "--summary",
            "Commit abc",
            "--source",
            "git-hook",
            "--ref",
            "sha:abc",
            "--metadata",
            '{"sha":"abc"}',
            "--ts",
            "2026-04-26T10:00:00Z",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo_id"] == "example-repo"
    assert Path(payload["ndjson_path"]).exists()

    listed = runner.invoke(cli, ["list", "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert rows[0]["refs"] == ["sha:abc"]
    assert rows[0]["metadata"] == {"sha": "abc"}
    assert rows[0]["event_id"] == rows[0]["id"]
    assert rows[0]["record_class"] == "observation"
    assert rows[0]["schema_version"] == 1
    assert rows[0]["origin_seq"] == 1
    assert rows[0]["payload"] == {
        "summary": "Commit abc",
        "detail": None,
        "refs": ["sha:abc"],
        "source": "git-hook",
        "metadata": {"sha": "abc"},
    }
    assert len(rows[0]["payload_sha256"]) == 64

    rendered = runner.invoke(cli, ["render", "--format", "ndjson"])
    assert rendered.exit_code == 0, rendered.output
    assert json.loads(rendered.output)["summary"] == "Commit abc"


def test_list_shows_full_type_and_ledger_context(repo_root: Path) -> None:
    runner = CliRunner()
    event_type = "workflow.session.completed"
    added = runner.invoke(
        cli,
        ["add", "--type", event_type, "--actor", "tester", "--summary", "Finished"],
    )
    assert added.exit_code == 0, added.output

    listed = runner.invoke(cli, ["list"])
    assert listed.exit_code == 0, listed.output
    assert str(repo_root / ".auditctl" / "auditctl.db") in listed.output
    assert "resolved via env: AUDITCTL_DB" in listed.output
    assert "workflow.session.completed" in listed.output
    assert "workflow.sessi " not in listed.output


def test_list_calls_out_parent_walk_resolution(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("AUDITCTL_DB", raising=False)
    monkeypatch.delenv("AUDITCTL_ARTIFACTS_ROOT", raising=False)
    nested = repo_root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)

    listed = CliRunner().invoke(cli, ["list"])
    assert listed.exit_code == 0, listed.output
    assert "parent-walk from" in listed.output
    assert str(repo_root / ".auditctl" / "auditctl.db") in listed.output


def test_list_unknown_type_is_actionable(repo_root: Path) -> None:
    result = CliRunner().invoke(cli, ["list", "--type", "nope"])
    assert result.exit_code == 3
    assert "error:" in result.output
    assert "no audit events found for type 'nope'" in result.output
    assert "fix: auditctl status" in result.output


def test_status_reports_counts_newest_and_drift(repo_root: Path) -> None:
    runner = CliRunner()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = runner.invoke(
        cli,
        ["add", "--type", "workflow.session.completed", "--actor", "tester", "--summary", "Finished", "--ts", now],
    )
    assert added.exit_code == 0, added.output

    status = runner.invoke(cli, ["status"])
    assert status.exit_code == 0, status.output
    assert str(repo_root / ".auditctl" / "auditctl.db") in status.output
    assert "workflow.session.completed" in status.output
    assert "newest event:" in status.output
    assert "shard/sqlite line count: matched" in status.output
    assert "next: auditctl list --limit 10" in status.output

    status_json = runner.invoke(cli, ["status", "--json"])
    assert status_json.exit_code == 0, status_json.output
    payload = json.loads(status_json.output)
    assert payload["schema"] == "auditctl.status/v1"
    assert payload["counts"]["last_24h"]["workflow.session.completed"] == 1
    assert payload["counts"]["last_7d"]["workflow.session.completed"] == 1


def test_status_accepts_fractional_event_timestamps(repo_root: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(
        cli,
        ["add", "--type", "decision", "--actor", "tester", "--summary", "Fractional",
         "--ts", "2026-11-17T18:40:21.500Z"],
    )
    assert added.exit_code == 0, added.output
    for args in (["status"], ["status", "--json"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output


def test_status_without_ledger_is_read_only(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("AUDITCTL_DB", raising=False)
    monkeypatch.delenv("AUDITCTL_ARTIFACTS_ROOT", raising=False)
    for args in (["status"], ["status", "--json"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert not (repo_root / ".auditctl").exists()
        if args[-1] == "--json":
            payload = json.loads(result.output)
            assert payload["counts"] == {"last_24h": {}, "last_7d": {}}
            assert payload["drift"]["sqlite_events"] == 0
        else:
            assert "No ledger yet." in result.output
            assert "next: auditctl add --help" in result.output


def test_list_known_type_without_matching_range_is_empty_success(repo_root: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(
        cli, ["add", "--type", "decision", "--actor", "tester", "--summary", "Earlier"]
    )
    assert added.exit_code == 0, added.output
    result = runner.invoke(
        cli, ["list", "--type", "decision", "--since", "2030-01-01T00:00:00Z"]
    )
    assert result.exit_code == 0, result.output
    assert "No audit events found." in result.output
    assert runner.invoke(
        cli, ["list", "--type", "decision", "--since", "2030-01-01T00:00:00Z", "--json"]
    ).output == "[]\n"


def test_status_surfaces_drift_as_needs_you(repo_root: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(
        cli, ["add", "--type", "decision", "--actor", "tester", "--summary", "Drift"]
    )
    assert added.exit_code == 0, added.output
    shard = next((repo_root / "_artifacts" / repo_root.name / "audit").glob("events-*.ndjson"))
    shard.write_text("", encoding="utf-8")
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "▲ needs you (1)" in result.output
    assert "▲ ledger drift: sqlite=1, shards=0" in result.output
    assert "next: auditctl rebuild --from-ndjson " in result.output
    assert result.output.rstrip().endswith("--dry-run")
    assert "Nothing needs you." not in result.output


def test_help_lists_unsectioned_commands_under_other() -> None:
    from auditctl.cli import AuditGroup

    original = AuditGroup.sections
    AuditGroup.sections = tuple((heading, tuple(name for name in names if name != "render"))
                                for heading, names in original)
    try:
        result = CliRunner().invoke(cli, ["--help"])
    finally:
        AuditGroup.sections = original
    assert result.exit_code == 0, result.output
    assert "Other:" in result.output
    assert "render" in result.output


def test_version_option_uses_package_version() -> None:
    import auditctl

    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert auditctl.__version__ in result.output


def test_add_rejects_invalid_metadata(repo_root: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["add", "--type", "x", "--actor", "a", "--summary", "s", "--metadata", "[]"],
    )
    assert result.exit_code != 0
    assert "metadata must be a JSON object" in result.output


def test_add_defaults_the_artifacts_root_to_the_resolved_repository(
    repo_root: Path, monkeypatch
) -> None:
    """An absent root is not an error; it is the normal case.

    Requiring it was the defect's precondition: every caller had to supply the root
    out of band, and on 2026-08-29 a shared hook supplied one naming a single
    repository for every repo that used it. A root that defaults to the resolved
    repository cannot be supplied wrongly, because it is not supplied at all.
    """
    monkeypatch.delenv("AUDITCTL_ARTIFACTS_ROOT", raising=False)
    result = CliRunner().invoke(cli, ["add", "--type", "x", "--actor", "a", "--summary", "s"])
    assert result.exit_code == 0, result.output
    shard = repo_root / "_artifacts" / repo_root.name / "audit"
    assert list(shard.glob("events-*.ndjson")), "shard must land under the resolved repo"


def test_add_refuses_an_artifacts_root_that_disagrees_with_the_index(
    repo_root: Path, monkeypatch, tmp_path: Path
) -> None:
    """Fail closed, do not prefer one half.

    This is the exact production shape: a correct index and a root pointing at a
    different repository. Both halves are individually valid, so nothing but a
    cross-check can catch it.
    """
    monkeypatch.setenv("AUDITCTL_ARTIFACTS_ROOT", str(tmp_path / "somewhere-else"))
    result = CliRunner().invoke(cli, ["add", "--type", "x", "--actor", "a", "--summary", "s"])
    assert result.exit_code != 0
    assert "does not agree with the resolved repository" in result.output
    assert "index-only" in result.output


def test_status_fresh_clone_points_at_a_rebuild_that_runs(repo_root: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(
        cli, ["add", "--type", "decision", "--actor", "tester", "--summary", "Committed"]
    )
    assert added.exit_code == 0, added.output
    ledger = Path(json.loads(runner.invoke(cli, ["status", "--json"]).output)["context"]["ledger_path"])
    ledger.unlink()

    payload = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert payload["needs_you"] == ["no sqlite index for 1 shard event(s)"]
    assert not ledger.exists()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "▲ no sqlite index for 1 shard event(s)" in result.output
    assert "No ledger yet." not in result.output
    assert not ledger.exists()

    step = payload["next"][0].split(" ", 1)[1]
    rebuilt = runner.invoke(cli, shlex.split(step))
    assert rebuilt.exit_code == 0, rebuilt.output
