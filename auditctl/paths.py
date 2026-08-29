from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditPaths:
    repo_root: Path
    repo_id: str
    db_path: Path
    # How the repository was decided. Carried, not recomputed by callers -- a caller
    # that re-derives this is doing the thing that caused the defect.
    source: str = "unknown"


@dataclass(frozen=True)
class AuditContext:
    """One atomic answer to "where does this write go", consumed rather than rebuilt.

    Every field derives from a single resolved root. The combination that caused the
    2026-08-29 misrouting -- an index resolved from the CWD and an artifacts root
    supplied by an unrelated caller -- is not representable here: `resolve_audit_context`
    refuses to build one. See docs/contracts/session-resolved-context.md.
    """

    repo_id: str
    repo_root: Path
    index_path: Path
    artifacts_root: Path
    resolution_source: str

    def shard_for(self, ts: str) -> Path:
        """The shard this context writes to. Never recompute this from parts."""
        return shard_path(self.artifacts_root, self.repo_id, ts)

    def as_record(self, published_from: Path) -> dict[str, str]:
        """This context, in the shape an event carries it.

        Resolving correctly is not enough on its own. `AUDITCTL_DB` can still relocate
        identity, index and shard *together*, and a redirect that moves all three leaves
        no contradiction for the fail-closed check above to find -- both stores validate
        clean afterwards, because both are clean. Measured 2026-08-29; see
        agentops docs/evidence/measurements/2026-08-29-coherent-context-redirect.md.

        What makes such a write recoverable is the same thing that made the August
        misrouting recoverable: a record of where it came from. That one was incoherent,
        so the mismatch between index and shard *was* the evidence. A coherent redirect
        has no such mismatch, so the origin has to be written down at the moment it is
        still known. `published_from` is that origin -- the directory the walk started
        at -- and `resolution_source` is how the answer was reached.

        This is deliberately a record and not a check. Publishing into another repository's
        store is a legitimate thing to do on purpose; refusing it would outlaw the
        override rather than account for it. auditctl records conformance, it does not
        state desired state.
        """
        return {
            "repo_id": self.repo_id,
            "repo_root": str(self.repo_root),
            "artifacts_root": str(self.artifacts_root),
            "published_from": str(published_from),
            "resolution_source": self.resolution_source,
        }


REPO_ID_FILE = ".auditctl-id"
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _worktree_main_root(repo_root: Path) -> Path | None:
    """The main repository backing a linked worktree, or None if this is not one.

    A worktree's `.git` is a *file* holding `gitdir: <main>/.git/worktrees/<name>`.
    Read it directly rather than shelling out: this runs inside publisher hooks whose
    PATH is the one thing that cannot be trusted.

    Worktrees are why `repo_id` was wrong in practice. A worktree has no `.auditctl`
    (it is gitignored), so the marker walk climbs past it: the agentops worktree under
    `_projects/vuoro-dispatch-ready/members/` resolved to `repo_id="dev"`, and one under
    $HOME resolved to its own basename and took its evidence with it when deleted.
    `_artifacts/wt-counter/` and `wt-review/` are what that leaves behind.
    """
    dot_git = repo_root / ".git"
    if not dot_git.is_file():
        return None
    try:
        text = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    match = re.match(r"gitdir:\s*(.+)$", text)
    if not match:
        return None
    gitdir = Path(match.group(1).strip())
    if not gitdir.is_absolute():
        gitdir = (repo_root / gitdir).resolve()
    # <main>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def _declared_repo_id(repo_root: Path) -> str | None:
    """A repository's own declaration of its identity, if it makes one.

    Identity derived from a directory basename is an accident of geography: it changes
    when the directory is renamed and differs between hosts that check out to different
    paths. A tracked `.auditctl-id` at the repo root travels with the repository and is
    identical in every worktree and on every host.

    It is not read from `.auditctl/`, which is gitignored and so would not travel, and
    not from an environment variable, which shared-scope code can set for repositories
    it knows nothing about -- the defect class this whole change exists to close.
    """
    candidate = repo_root / REPO_ID_FILE
    try:
        if not candidate.is_file():
            return None
        value = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not value:
        return None
    if not _REPO_ID_RE.match(value):
        raise ValueError(
            f"{candidate} declares an unusable repo id: {value!r}. "
            "It becomes a directory name under the artifacts root, so it must match "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,63} -- no separators, no traversal."
        )
    return value


def _find_upward(start: Path, predicate) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if predicate(path):
            return path
    return None


def _repo_root_from_db_path(db_path: Path) -> Path | None:
    resolved = db_path.expanduser().resolve()
    if resolved.parent.name == ".auditctl":
        return resolved.parent.parent
    return _find_upward(resolved.parent, lambda p: (p / ".auditctl").is_dir() or (p / ".git").exists())


def resolve_paths(cwd: Path | None = None, env: dict[str, str] | None = None) -> AuditPaths:
    env_map = os.environ if env is None else env
    start = Path.cwd() if cwd is None else cwd
    explicit_db = env_map.get("AUDITCTL_DB")

    if explicit_db:
        db_path = Path(explicit_db).expanduser()
        repo_root = _repo_root_from_db_path(db_path)
        if repo_root is None:
            raise ValueError("cannot resolve repo root from AUDITCTL_DB; run inside the repo or use .auditctl/auditctl.db")
        return AuditPaths(
            repo_root=repo_root,
            repo_id=_declared_repo_id(repo_root) or repo_root.name,
            db_path=db_path,
            source="explicit-db",
        )

    # A linked worktree belongs to its main repository, whatever indexes sit above it.
    # This is checked before the marker walk precisely because the worktree has no index
    # of its own, so the walk would otherwise climb past it into an unrelated workspace.
    worktree_root = _find_upward(start, lambda p: _worktree_main_root(p) is not None)
    if worktree_root is not None:
        main_root = _worktree_main_root(worktree_root)
        assert main_root is not None
        return AuditPaths(
            repo_root=main_root,
            repo_id=_declared_repo_id(main_root) or main_root.name,
            db_path=main_root / ".auditctl" / "auditctl.db",
            source="worktree-main",
        )

    marker_root = _find_upward(start, lambda p: (p / ".auditctl" / "auditctl.db").exists())
    if marker_root is not None:
        return AuditPaths(
            repo_root=marker_root,
            repo_id=_declared_repo_id(marker_root) or marker_root.name,
            db_path=marker_root / ".auditctl" / "auditctl.db",
            source="index-marker",
        )

    git_root = _find_upward(start, lambda p: (p / ".git").exists())
    if git_root is None:
        raise ValueError("not inside an auditctl-enabled repo; set AUDITCTL_DB.")
    return AuditPaths(
        repo_root=git_root,
        repo_id=_declared_repo_id(git_root) or git_root.name,
        db_path=git_root / ".auditctl" / "auditctl.db",
        source="git-marker",
    )


def resolve_audit_context(
    cwd: Path | None = None, env: dict[str, str] | None = None
) -> AuditContext:
    """Resolve identity, index and artifacts root together, or fail.

    `resolve_paths` derives the index and `repo_id` by walking up from the CWD, and the
    artifacts root used to be read independently from the environment. Two independent
    resolutions that happen to agree are not one resolution: on 2026-08-29 a shared hook
    supplied a root naming one repository while each session indexed at its own, and 13
    events were written to a correct index and a shard under someone else's repo. Nothing
    detected it, because neither half was wrong on its own terms.

    So the root is not an independent input. It defaults to the resolved repo root, and an
    explicit `AUDITCTL_ARTIFACTS_ROOT` may only *confirm* that root -- never redirect it.
    A disagreement is an error, not a preference to be silently applied.
    """
    env_map = os.environ if env is None else env
    paths = resolve_paths(cwd=cwd, env=env_map)

    source = paths.source
    if _declared_repo_id(paths.repo_root):
        source = f"{source}+declared-id"

    repo_root = paths.repo_root.expanduser().resolve()
    raw_root = env_map.get("AUDITCTL_ARTIFACTS_ROOT")
    if raw_root:
        explicit_root = Path(raw_root).expanduser().resolve()
        # Ancestor-or-equal, not equality. Two conventions are in deliberate use across
        # this fleet and both are coherent:
        #
        #   co-rooted  root == repo_root          (agentops, vuoro, scribectl)
        #   pooled     root is an ancestor of it  (sprintctl, kctl, cred-broker, ...)
        #
        # Pooling is safe because `repo_id` namespaces the shard directory beneath the
        # shared root, so the pairing stays unambiguous. What is never safe is a root
        # *below* the resolved repository, or off its line entirely: that writes the
        # shard under some other repository's tree while the index stays here. That is
        # exactly the 2026-08-29 geometry -- repo_id `dev` indexed at /projects/dev with
        # a root of /projects/dev/agentops, a descendant.
        #
        # Equality alone would have outlawed five repositories' committed .envrc on the
        # first `add`, which is this contract's own falsifier: a fail-closed rule people
        # route around is worse than the silent preference it replaced.
        if explicit_root != repo_root and not repo_root.is_relative_to(explicit_root):
            raise ValueError(
                "AUDITCTL_ARTIFACTS_ROOT does not agree with the resolved repository:\n"
                f"  artifacts root : {explicit_root}\n"
                f"  repository     : {repo_root}  (via {source})\n"
                f"  repo_id        : {paths.repo_id}\n"
                "The root must be the repository itself or an ancestor of it. This one is "
                "neither, so shards would be written under a tree that does not hold the "
                "index they belong to, and `rebuild` would report them as index-only. "
                "Unset AUDITCTL_ARTIFACTS_ROOT to use the resolved repository, or run "
                "from within the repository you intend to write to."
            )
        source = f"{source}+explicit-root"
        artifacts_root = explicit_root
    else:
        artifacts_root = repo_root

    return AuditContext(
        repo_id=paths.repo_id,
        repo_root=repo_root,
        index_path=paths.db_path,
        artifacts_root=artifacts_root,
        resolution_source=source,
    )


def require_artifacts_root(env: dict[str, str] | None = None) -> Path:
    env_map = os.environ if env is None else env
    raw = env_map.get("AUDITCTL_ARTIFACTS_ROOT")
    if not raw:
        raise ValueError("AUDITCTL_ARTIFACTS_ROOT is required for audit writes.")
    return Path(raw).expanduser()


def shard_path(artifacts_root: Path, repo_id: str, ts: str) -> Path:
    day = ts[:10]
    return artifacts_root / "_artifacts" / repo_id / "audit" / f"events-{day}.ndjson"

