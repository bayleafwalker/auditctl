from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditPaths:
    repo_root: Path
    repo_id: str
    db_path: Path


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
        return AuditPaths(repo_root=repo_root, repo_id=repo_root.name, db_path=db_path)

    marker_root = _find_upward(start, lambda p: (p / ".auditctl" / "auditctl.db").exists())
    if marker_root is not None:
        return AuditPaths(
            repo_root=marker_root,
            repo_id=marker_root.name,
            db_path=marker_root / ".auditctl" / "auditctl.db",
        )

    git_root = _find_upward(start, lambda p: (p / ".git").exists())
    if git_root is None:
        raise ValueError("not inside an auditctl-enabled repo; set AUDITCTL_DB.")
    return AuditPaths(
        repo_root=git_root,
        repo_id=git_root.name,
        db_path=git_root / ".auditctl" / "auditctl.db",
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

    if env_map.get("AUDITCTL_DB"):
        source = "explicit-db"
    elif (paths.repo_root / ".auditctl" / "auditctl.db").exists():
        source = "index-marker"
    else:
        source = "git-marker"

    repo_root = paths.repo_root.expanduser().resolve()
    raw_root = env_map.get("AUDITCTL_ARTIFACTS_ROOT")
    if raw_root:
        explicit_root = Path(raw_root).expanduser().resolve()
        if explicit_root != repo_root:
            raise ValueError(
                "AUDITCTL_ARTIFACTS_ROOT does not agree with the resolved repository:\n"
                f"  artifacts root : {explicit_root}\n"
                f"  repository     : {repo_root}  (via {source})\n"
                f"  repo_id        : {paths.repo_id}\n"
                "Shards would be written under a repository that does not hold the index "
                "they belong to, so `rebuild` would report them as index-only. Unset "
                "AUDITCTL_ARTIFACTS_ROOT to use the resolved repository, or run from "
                "within the repository you intend to write to."
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

