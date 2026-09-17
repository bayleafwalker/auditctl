from __future__ import annotations

from pathlib import Path

import pytest

from auditctl.paths import resolve_paths, shard_path


def test_resolve_paths_from_git_repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUDITCTL_DB", raising=False)
    paths = resolve_paths(cwd=repo_root / "subdir")
    assert paths.repo_root == repo_root
    assert paths.repo_id == "example-repo"
    assert paths.db_path == repo_root / ".auditctl" / "auditctl.db"


def test_resolve_paths_from_explicit_db(repo_root: Path) -> None:
    paths = resolve_paths()
    assert paths.repo_id == "example-repo"
    assert paths.db_path == repo_root / ".auditctl" / "auditctl.db"


def test_shard_path_uses_daily_repo_layout(tmp_path: Path) -> None:
    assert shard_path(tmp_path, "repo", "2026-04-26T10:00:00Z") == (
        tmp_path / "_artifacts" / "repo" / "audit" / "events-2026-04-26.ndjson"
    )


def test_resolve_paths_prefers_nearer_git_over_farther_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo with no local index yet must not resolve into an ancestor's index.

    Reproduces the shared-workspace geometry: a pooled `.auditctl/auditctl.db` sits at
    the workspace root (as the installed publisher writes by default), and a repository
    nested beneath it -- with its own `.git` but no index of its own -- must still root
    at itself, not at the outer workspace.
    """
    monkeypatch.delenv("AUDITCTL_DB", raising=False)
    workspace = tmp_path / "workspace"
    (workspace / ".auditctl").mkdir(parents=True)
    (workspace / ".auditctl" / "auditctl.db").touch()
    repo = workspace / "inner-repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "sub"
    sub.mkdir()

    paths = resolve_paths(cwd=sub)

    assert paths.repo_root == repo
    assert paths.repo_id == "inner-repo"
    assert paths.db_path == repo / ".auditctl" / "auditctl.db"
    assert paths.source == "git-marker"

