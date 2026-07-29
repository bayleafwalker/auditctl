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

- Run targeted `pytest` checks before committing; behavior changes must include tests.
- Keep auditctl local-first: no Postgres backend, daemon, or service.
- Publishers call the `auditctl` binary as a subprocess; do not add a Python client API unless an accepted plan changes that boundary.
- Preserve stable event IDs and idempotent rebuild semantics.

## Stateful protocol verification

The governing protocol draft is `docs/protocols/audit-write-and-rebuild.md`; repo-specific verification rules are in `.agents/overlays/auditctl.state-protocols.md`.

Use the shared `verify-state-protocols` skill when changes affect SQLite/NDJSON ordering, file locking, crash recovery, event identity, rebuild, or concurrent publishers. Default to Depth 2 for dual-write and concurrent-writer changes. `survey` and `reconcile` are read-only; production-semantic repairs require a separate authorized build action.

Do not describe SQLite and NDJSON as one atomic transaction. Successful responses establish both copies, while crash and commit-failure windows can leave NDJSON ahead; rebuild is the convergence mechanism.

Verification must use temporary repositories and artifact roots, record injected fault points, and never publish test events into shared workspace artifacts.

The machine-readable routing and hook policy is `auditctl.dispatch.json`. Validate reusable packets with `python /projects/dev/agentops/templates/dispatch/scripts/validate_verification_artifacts.py --root .`.

<!-- agentops-project-pointer:start -->
See `.agents/project.generated.md` for cross-repo project context (agentops-managed; do not hand-edit).
<!-- agentops-project-pointer:end -->

<!-- agentops-environment-pointer:start -->
See `.agents/environment.generated.md` for the active Vuoro environment's constraints and runbooks (agentops-managed; do not hand-edit).
<!-- agentops-environment-pointer:end -->
