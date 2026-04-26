from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditPaths:
    repo_root: Path
    repo_id: str
    db_path: Path


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


def require_artifacts_root(env: dict[str, str] | None = None) -> Path:
    env_map = os.environ if env is None else env
    raw = env_map.get("AUDITCTL_ARTIFACTS_ROOT")
    if not raw:
        raise ValueError("AUDITCTL_ARTIFACTS_ROOT is required for audit writes.")
    return Path(raw).expanduser()


def shard_path(artifacts_root: Path, repo_id: str, ts: str) -> Path:
    day = ts[:10]
    return artifacts_root / "_artifacts" / repo_id / "audit" / f"events-{day}.ndjson"

