from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from auditctl.cli import cli


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
