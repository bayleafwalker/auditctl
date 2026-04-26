from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUDITCTL_DB", str(repo / ".auditctl" / "auditctl.db"))
    monkeypatch.setenv("AUDITCTL_ARTIFACTS_ROOT", str(tmp_path))
    return repo


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AUDITCTL_DB", "AUDITCTL_ARTIFACTS_ROOT"):
        monkeypatch.delenv(key, raising=False)

