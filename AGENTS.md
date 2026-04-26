# AGENTS.md - auditctl

> Environment reference: `/projects/dev/AGENTS.md`.

## Tech Stack

Primary language: Python >= 3.11. CLI framework: Click. Storage: sqlite plus NDJSON shards. Tests: pytest.

## Environment

| Variable | Purpose |
|---|---|
| `AUDITCTL_DB` | Optional explicit sqlite path. Defaults to `<repo>/.auditctl/auditctl.db` when run inside a git repo. |
| `AUDITCTL_ARTIFACTS_ROOT` | Required for writes. Root containing `_artifacts/<repo_id>/audit/`. |

For the `/projects/dev` workspace, `AUDITCTL_ARTIFACTS_ROOT=/projects/dev`.

## Development Workflow

- Run `pytest` before committing.
- Behavior changes must include tests.
- Keep auditctl local-first: no pg backend, daemon, or service.
- Publishers call the `auditctl` binary as a subprocess; do not add a Python client API unless a later plan changes that.

