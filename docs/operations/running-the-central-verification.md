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

### When the nix daemon is unavailable

`nix shell` needs `nix-daemon`; an already-realized store path does not. On 2026-08-29
the daemon stopped answering — healthy process, listening socket, empty backlog, no
errors logged, but every connection reset before it reached `accept()`, and restarting
it needs root this account does not have. That looked like a hard blocker and was not:

```bash
PGBIN=$(for d in /nix/store/*postgresql-18*/bin; do
          [ -x "$d/initdb" ] && [ -x "$d/pg_ctl" ] &&
          "$d/postgres" --version | awk -v p="$d" '{print $3, p}'
        done | sort -V | tail -1 | awk '{print $2}')
PATH="$PGBIN:$PATH" .venv/bin/python -m pytest tests/ -q
```

Result: **133 passed, 1 skipped** — the same PostgreSQL 18.6, the same nine central
integration tests, no daemon involved. The store is a directory of realized paths; the
daemon is only needed to *build or fetch* one that is not there yet.

**But do not read that as durable.** An earlier version of this page said "once a version
has been used successfully even once, it stays usable". That was falsified the same day:
the run above succeeded at 19:4x, and by 22:4x `/nix/store` held **no postgresql path at
all** — the exact directory named above was gone, and the same command failed with ten
skips instead of one. A realized path survives until something collects it, which is not a
guarantee and not under this procedure's control. The daemon was healthy again by then, so
`nix shell` simply re-fetched it and the suite was green at 133 again.

So the ordering is: try the daemon first, fall back to a realized path when it is down, and
**check the skip count either way** — the fallback going missing looks exactly like the
fallback working, because both print a green summary.

The general lesson is the one this page already carries in another form: a tool being
unavailable is not the same fact as a capability being unavailable, and the difference
is usually one invocation away. Check whether the thing you need is already on disk
before recording a blocker.

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
| 0.1.3 | `rebuild rejected [index_only_events]`, **exit 1** |

That gap is now closed end to end: 0.1.3 is installed on the workstation, pinned by the
vuoro composition, and served by vuoro-shared via vuoro-service 0.1.57.

### Correction: the "40 index-only events" reported here were an artifact of the check

**The original text of this section was wrong and is retracted.** It reported 40 events
across four publishers as unbacked, and called it an open publisher defect. Both the
number and the diagnosis were produced by a mis-scoped invocation: `--from-ndjson` pointed
at the `agentops`-scope shards while the resolved index was the workspace `dev`-scope
store. Comparing an index against a *different scope's* shards manufactures an arbitrary
index-only count. The four publisher names were simply the sources present in the index.

What was actually wrong was smaller and different in kind: **13 events, one publisher, and
misrouted rather than missing.** `artifacts-root.default` in agentops named a single
repository, and the hook that reads it is symlinked into every repo, so sessions indexed at
their own root and appended under agentops. Fixed in agentops `5757779` and `a44f01d`; the
events were merged back verbatim. Set and digest equality were verified across the repair:
no id lost, no line altered, no duplicate, and `index ⊆ shards` with zero index-only.

Two lessons, both cheaper to read than to rediscover:

- **Pair an index only with its own scope's shards.** `rebuild` cannot tell a wrong `--from-ndjson`
  from real loss, and its message reads like loss either way. Confirm `repo_id` and the root
  agree before believing any count.
- **Re-run the check after applying a fix, never infer success from having applied it.** The
  first repair here looked correct and was still writing strays; only re-running found it.
  This is a required postcondition of a repair, not a good habit.

The release above stands independently of all this: it was justified by the guard existing
only on `main` while the installed 0.1.2 reported success on an index-only store, which is
true regardless of how many events any particular store held.
