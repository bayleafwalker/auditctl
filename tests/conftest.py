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
    # Co-rooted, deliberately. This fixture used to point the artifacts root at
    # `tmp_path` while the index resolved to `tmp_path/example-repo` -- the exact split
    # that misrouted 13 events in production on 2026-08-29. With the split normalised
    # into the fixture, no test in this suite could have caught it.
    monkeypatch.setenv("AUDITCTL_ARTIFACTS_ROOT", str(repo))
    return repo


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AUDITCTL_DB", "AUDITCTL_ARTIFACTS_ROOT"):
        monkeypatch.delenv(key, raising=False)

