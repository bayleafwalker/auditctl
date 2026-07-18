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


def test_add_requires_artifacts_root(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("AUDITCTL_ARTIFACTS_ROOT", raising=False)
    result = CliRunner().invoke(cli, ["add", "--type", "x", "--actor", "a", "--summary", "s"])
    assert result.exit_code != 0
    assert "AUDITCTL_ARTIFACTS_ROOT is required" in result.output
