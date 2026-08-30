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

