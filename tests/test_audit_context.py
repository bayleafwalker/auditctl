"""Resolution must be atomic, fail closed, and say why.

These are written against the production defect of 2026-08-29 rather than against the
implementation. `resolve_paths` derived the index and `repo_id` from the CWD while the
artifacts root came from the environment; a shared hook supplied a root naming one
repository, so every session indexed at its own root and appended under that one. Neither
half was wrong on its own terms, which is why nothing caught it for a month.

So each test below asserts a property of the *pair*. A test that only checks one half
would have passed throughout the defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auditctl.paths import resolve_audit_context


def _repo(root: Path, name: str, *, index: bool = False, git: bool = True) -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    if git:
        (repo / ".git").mkdir(exist_ok=True)
    if index:
        (repo / ".auditctl").mkdir(exist_ok=True)
        (repo / ".auditctl" / "auditctl.db").touch()
    return repo


def test_index_and_shard_always_share_a_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "solo", index=True)
    context = resolve_audit_context(cwd=repo, env={})

    assert context.artifacts_root == context.repo_root
    # The shard must sit under the same root that holds the index it is written beside.
    assert context.index_path.parent.parent == context.repo_root
    assert context.shard_for("2026-08-29T10:00:00Z").is_relative_to(context.repo_root)


def test_an_absent_root_is_the_normal_case_not_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "solo", index=True)
    context = resolve_audit_context(cwd=repo, env={})

    assert context.artifacts_root == repo
    assert context.resolution_source == "index-marker"


def test_a_root_that_disagrees_with_the_index_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "solo", index=True)
    elsewhere = _repo(tmp_path, "elsewhere", index=True)

    with pytest.raises(ValueError) as excinfo:
        resolve_audit_context(cwd=repo, env={"AUDITCTL_ARTIFACTS_ROOT": str(elsewhere)})

    message = str(excinfo.value)
    # Both halves must be named. A message that reports only one is not diagnosable
    # without redoing the resolution, which is what produced the wrong answer.
    assert str(elsewhere) in message and str(repo) in message
    assert "index-only" in message


def test_a_root_that_merely_confirms_the_index_is_accepted_and_recorded(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "solo", index=True)
    context = resolve_audit_context(cwd=repo, env={"AUDITCTL_ARTIFACTS_ROOT": str(repo)})

    assert context.artifacts_root == repo
    assert context.resolution_source == "index-marker+explicit-root"


def test_an_index_beats_a_nearer_git_in_a_nested_repository(tmp_path: Path) -> None:
    """The case that defeated the first repair.

    A workspace holding the index contains repos that have a `.git` and no index of
    their own. Stopping at the inner `.git` roots the shard in that repo while the
    index stays at the workspace -- the same split, one directory down.
    """
    workspace = _repo(tmp_path, "workspace", index=True, git=False)
    inner = _repo(workspace, "inner-repo")
    (inner / "sub").mkdir()

    for cwd in (inner, inner / "sub"):
        context = resolve_audit_context(cwd=cwd, env={})
        assert context.repo_root == workspace
        assert context.repo_id == "workspace"
        assert context.artifacts_root == workspace


def test_a_repo_with_no_index_falls_back_to_git_and_still_co_roots(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "no-index", index=False)
    context = resolve_audit_context(cwd=repo, env={})

    assert context.resolution_source == "git-marker"
    assert context.artifacts_root == context.repo_root == repo
    assert context.index_path == repo / ".auditctl" / "auditctl.db"


def test_an_explicit_db_decides_the_root_and_the_shard_follows_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "solo", index=True)
    other = _repo(tmp_path, "other", index=True)

    context = resolve_audit_context(
        cwd=repo, env={"AUDITCTL_DB": str(other / ".auditctl" / "auditctl.db")}
    )

    assert context.repo_root == other
    assert context.repo_id == "other"
    assert context.artifacts_root == other  # the shard follows the index, not the CWD
    assert context.resolution_source == "explicit-db"


def test_contradictory_explicit_db_and_root_fail_closed(tmp_path: Path) -> None:
    """Both overrides explicit and mutually incompatible -- the sharpest case.

    Each is individually honoured by the old code: the index goes where AUDITCTL_DB
    says, the shard goes where AUDITCTL_ARTIFACTS_ROOT says, and they are different
    repositories. This must be an error, not a silent preference for either.
    """
    repo = _repo(tmp_path, "solo", index=True)
    other = _repo(tmp_path, "other", index=True)

    with pytest.raises(ValueError, match="does not agree with the resolved repository"):
        resolve_audit_context(
            cwd=repo,
            env={
                "AUDITCTL_DB": str(other / ".auditctl" / "auditctl.db"),
                "AUDITCTL_ARTIFACTS_ROOT": str(repo),
            },
        )


def test_a_shared_publisher_cannot_redirect_another_repositorys_shards(tmp_path: Path) -> None:
    """The production defect, reproduced as a test.

    A hook shared by every repository exported one root for all of them. Under the old
    contract each session accepted it and wrote a shard under a repo it had nothing to
    do with. Two different repos must never resolve to one artifacts root.
    """
    shared_root = _repo(tmp_path, "the-one-repo-the-hook-named", index=True)
    repo_a = _repo(tmp_path, "repo-a", index=True)
    repo_b = _repo(tmp_path, "repo-b", index=True)

    for repo in (repo_a, repo_b):
        with pytest.raises(ValueError):
            resolve_audit_context(
                cwd=repo, env={"AUDITCTL_ARTIFACTS_ROOT": str(shared_root)}
            )

    a = resolve_audit_context(cwd=repo_a, env={})
    b = resolve_audit_context(cwd=repo_b, env={})
    assert a.artifacts_root != b.artifacts_root
    assert a.shard_for("2026-08-29T10:00:00Z") != b.shard_for("2026-08-29T10:00:00Z")


def test_context_is_immutable(tmp_path: Path) -> None:
    context = resolve_audit_context(cwd=_repo(tmp_path, "solo", index=True), env={})
    with pytest.raises(Exception):
        context.artifacts_root = tmp_path  # type: ignore[misc]
