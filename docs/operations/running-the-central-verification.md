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

`auditctl` 0.1.3 was blocked on this and is no longer blocked by it. Note that
`tests/test_release_contract.py` pins the version literally
(`assert auditctl.__version__ == "0.1.2"`), so a bump updates that assertion together with
`pyproject.toml` and the adapter/schema-runtime digest locks the same test validates.

Releasing matters here rather than being housekeeping: the `rebuild` guard that refuses
index-only events (`d88a34c`) exists only on `main`. The installed 0.1.2 still reports
`Validated 5 shard(s): 51 event(s).` for an index holding 52 — success, while silently
dropping the event. Until 0.1.3 ships, the D3 gate is correct in source and absent
everywhere it runs.
