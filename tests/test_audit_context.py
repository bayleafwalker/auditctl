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


def test_a_pooled_root_above_the_repository_is_legitimate_and_accepted(tmp_path: Path) -> None:
    """Two conventions coexist deliberately; equality alone would outlaw one of them.

    Co-rooted repos set the root to the repo (agentops, vuoro, scribectl). Pooled repos
    set it to a shared ancestor with a repo-local index (sprintctl, kctl, cred-broker,
    bindery-core, and auditctl itself). Pooling is coherent because `repo_id` namespaces
    the shard directory beneath the shared root.

    An equality check would have failed the first `add` in five repositories whose .envrc
    is committed and in use -- firing this contract's own falsifier about fail-closed
    rules that callers route around.
    """
    workspace = tmp_path / "workspace"
    repo = _repo(workspace, "pooled-repo", index=True)

    context = resolve_audit_context(
        cwd=repo, env={"AUDITCTL_ARTIFACTS_ROOT": str(workspace)}
    )

    assert context.repo_id == "pooled-repo"
    assert context.artifacts_root == workspace
    assert context.index_path == repo / ".auditctl" / "auditctl.db"
    # Namespaced beneath the shared root, so two pooled repos cannot collide.
    assert context.shard_for("2026-08-29T10:00:00Z") == (
        workspace / "_artifacts" / "pooled-repo" / "audit" / "events-2026-08-29.ndjson"
    )


def test_a_root_below_the_repository_is_the_defect_geometry_and_is_refused(tmp_path: Path) -> None:
    """The 2026-08-29 shape, stated as geometry rather than as an instance.

    repo_id `dev` indexed at /projects/dev with a root of /projects/dev/agentops -- a
    *descendant*. The shard lands inside another repository's tree while the index stays
    put. Pooling roots upward; this roots downward, and the two must not be confused.
    """
    workspace = _repo(tmp_path, "workspace", index=True, git=False)
    inner = _repo(workspace, "some-other-repo", index=True)

    with pytest.raises(ValueError, match="must be the repository itself or an ancestor"):
        resolve_audit_context(cwd=workspace, env={"AUDITCTL_ARTIFACTS_ROOT": str(inner)})


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


# --- identity ----------------------------------------------------------------------
# `repo_id` names the shard directory, so it is the durable identity of a body of
# evidence. Deriving it from a directory basename makes it an accident of geography:
# it changes when the directory is renamed, differs between hosts that check out to
# different paths, and for a worktree it is either wrong or transient.


def _worktree(main: Path, at: Path, name: str) -> Path:
    """A linked worktree, in the shape git actually creates: `.git` is a file."""
    at.mkdir(parents=True, exist_ok=True)
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    (at / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / name}\n", encoding="utf-8"
    )
    return at


def test_a_worktree_is_attributed_to_its_main_repository(tmp_path: Path) -> None:
    """The verified production failure.

    A worktree has no `.auditctl` -- it is gitignored -- so the marker walk climbed
    past it. The agentops worktree under `_projects/.../members/` resolved to
    `repo_id="dev"`, blending its evidence into the workspace pool under the wrong
    identity.
    """
    workspace = _repo(tmp_path, "workspace", index=True, git=False)
    main = _repo(tmp_path, "realrepo", index=True)
    tree = _worktree(main, workspace / "members" / "realrepo", "realrepo")

    context = resolve_audit_context(cwd=tree, env={})

    assert context.repo_id == "realrepo"
    assert context.repo_root == main
    assert context.resolution_source == "worktree-main"
    # Evidence belongs in the durable home, not the disposable checkout.
    assert context.index_path == main / ".auditctl" / "auditctl.db"
    assert not context.shard_for("2026-08-29T10:00:00Z").is_relative_to(tree)


def test_a_worktree_outside_the_workspace_does_not_take_its_evidence_with_it(
    tmp_path: Path,
) -> None:
    """The second live case: a worktree under $HOME resolved to its own basename,
    so its shards died with the checkout. `_artifacts/wt-counter/` is that residue."""
    main = _repo(tmp_path, "realrepo", index=True)
    tree = _worktree(main, tmp_path / "elsewhere" / "realrepo-some-branch", "wt")

    context = resolve_audit_context(cwd=tree, env={})

    assert context.repo_id == "realrepo"
    assert context.repo_root == main


def test_a_declared_id_beats_the_directory_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "checked-out-under-some-other-name", index=True)
    (repo / ".auditctl-id").write_text("stable-identity\n", encoding="utf-8")

    context = resolve_audit_context(cwd=repo, env={})

    assert context.repo_id == "stable-identity"
    assert "declared-id" in context.resolution_source
    assert context.shard_for("2026-08-29T10:00:00Z").parent.parent.name == "stable-identity"


def test_a_declared_id_may_not_escape_the_artifacts_root(tmp_path: Path) -> None:
    """`repo_id` becomes a path segment, so a declaration is untrusted input."""
    repo = _repo(tmp_path, "solo", index=True)
    (repo / ".auditctl-id").write_text("../../etc\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unusable repo id"):
        resolve_audit_context(cwd=repo, env={})


def test_identity_is_not_taken_from_the_environment(tmp_path: Path) -> None:
    """Shared-scope code sets env vars for repos it knows nothing about -- that is the
    defect class. Identity must come from the repository, never from the caller."""
    repo = _repo(tmp_path, "solo", index=True)

    context = resolve_audit_context(cwd=repo, env={"AUDITCTL_REPO_ID": "something-else"})

    assert context.repo_id == "solo"
