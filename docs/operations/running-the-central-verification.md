# Running the central verification

`tests/test_central_pg_integration.py` needs a real PostgreSQL **server**, not just
`libpq`. Its fixture skips unless both `initdb` and `pg_ctl` are on `PATH`:

```python
if not all(shutil.which(command) for command in ("initdb", "pg_ctl")):
    pytest.skip("PostgreSQL server binaries are required for central integration tests")
```

It then builds a disposable cluster in a temp directory with `--auth=trust` on an
ephemeral port, so nothing persistent is created and no existing database is touched.

## Getting the binaries without root

This was recorded as a release blocker — a version bump needs the central verification
re-run, and the workstation was believed to lack the binaries. It does lack a system
PostgreSQL, and `/usr/bin/pg_config` is present only because `libpq` is installed, which
is *not* enough: `initdb` and `pg_ctl` ship with the server package.

Nix supplies them per-command, with no system install and no `sudo`:

```bash
nix shell nixpkgs#postgresql --command bash -c '.venv/bin/python -m pytest tests/ -q'
```

Verified on the workstation 2026-08-29 with PostgreSQL 18.6:

| Invocation | Result |
|---|---|
| `pytest tests/` | 104 passed, **10 skipped** |
| `nix shell nixpkgs#postgresql --command … pytest tests/` | **113 passed, 1 skipped** |

The one remaining skip is legitimate and unrelated —
`tests/test_release_contract.py:90`, "release wheel is built by the release workflow".

Nine of the ten skips were the central integration suite. They pass.

## Why this matters beyond convenience

The skip is silent by design — pytest reports success with a skip count, and a reader
scanning for a red line sees none. So "the central verification cannot run here" and "the
central verification passes here" produced the same green summary, and the blocker
survived on the strength of that. Check the skip count, not just the exit status; a
skipped gate is a gate that did not run.

## Consequence for the release

0.1.3 was blocked on this, was unblocked by it, and **shipped on 2026-08-29**
(`auditctl-v0.1.3`, wheel sha256 `f3d389ad...`). The section below is kept because the
bump procedure recurs, not because the release is still pending.

`tests/test_release_contract.py` pins the version literally, so a bump updates that
assertion together with `pyproject.toml` and the adapter/schema-runtime digest locks the
same test validates. A bump also touches `pyproject.toml` and `README.md`, which are
*inside* the two attested implementation trees in `tests/test_verification_contracts.py` —
so it necessarily invalidates both packet digests. Regenerate them **and** re-measure the
packet evidence fields, as 6131036 did; recomputing a digest without re-running the
verification would re-attest a result to a tree it was never measured on.

Releasing mattered here rather than being housekeeping. Before 0.1.3, the `rebuild` guard
that refuses index-only events (`d88a34c`) existed only on `main`, so the D3 gate was
correct in source and absent everywhere it actually ran. Measured on the workstation
against the real agentops store, same command, both versions:

| Installed | Result |
|---|---|
| 0.1.2 | validated the shards, **exit 0** |
| 0.1.3 | `rebuild rejected [index_only_events]` — 40 events no shard carries, **exit 1** |

That gap is now closed end to end: 0.1.3 is installed on the workstation, pinned by the
vuoro composition, and served by vuoro-shared via vuoro-service 0.1.57.

### The 40 index-only events are a separate, open finding

The guard did not create them; it revealed them. Some publisher is indexing without
appending to a shard — sources `claude-hook`, `friction-skill`, `hybrid-dispatch` and
`metanarrative`, on 2026-08-26 and 2026-08-29. Shards are authoritative, so those events
are currently unbacked. Find the publisher and re-emit; do not reach for
`--allow-index-only`, which accepts the loss rather than repairing it.
