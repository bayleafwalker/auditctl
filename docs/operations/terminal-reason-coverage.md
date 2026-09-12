# Terminal-reason coverage

Status: operations note, 2026-09-12.

`validation.TERMINAL_REASON_CODES` is a closed vocabulary of seven codes. The write path
rejects anything outside it. Nothing in this repository could say which of the seven had
ever actually been produced, because the validator answers *is this code allowed* and the
useful question is *has anything ever emitted it*. A code that passes the first test and
fails the second is a contract branch no publisher exercises.

`auditctl coverage terminal-reason` answers the second question by counting.

```bash
auditctl coverage terminal-reason --root /projects/dev
auditctl coverage terminal-reason --root /projects/dev --json
auditctl coverage terminal-reason --shards /projects/dev/_artifacts/dev/audit --strict
```

`--root` discovers both store shapes beneath a workspace: pooled shards at
`<root>/_artifacts/<repo_id>/audit/events-*.ndjson` and per-repository indexes at
`<repo>/.auditctl/auditctl.db`. `--index` and `--shards` name stores explicitly. With no
target it falls back to the store the current directory would *write* to, resolved through
`resolve_audit_context` rather than re-derived from parts. `--strict` exits non-zero when
any declared code is unwritten, for use as a gate.

## Measured, 2026-09-12

Over 11 indexes and 49 shards on this workstation — 6306 rows and lines read, 2003
distinct events carrying a `terminal_reason`, spanning `2026-08-29T06:03:40Z` to
`2026-09-12T17:22:48Z`:

| code | count | first seen | last seen |
| --- | --- | --- | --- |
| `completed` | 1999 | 2026-08-30 | 2026-09-12 |
| `crash-inferred` | 3 | 2026-08-29 | 2026-08-30 |
| `cancelled` | 1 | 2026-09-09 | 2026-09-09 |
| `process-exit` | 0 | never | never |
| `start-failed` | 0 | never | never |
| `timeout` | 0 | never | never |
| `usage-limit` | 0 | never | never |

**Four of seven, not six of seven.** The gap was previously recorded as six unwritten
codes. That was true when it was written and is not true now: `crash-inferred` and
`cancelled` have each since been observed, both from `dispatch.exit` events written by
`claude-hook`. The remaining four have never been written anywhere on this host.

`usage-limit` is the one that cost something. It is the code a commissioning incident
needed and could not find, and it is still at zero — which now means something precise:
the SubagentStop hook, the only live producer of `dispatch.exit`, has never once
classified a termination as a usage limit. That is a finding about the publisher, not
about this store.

Two counting rules make the number trustworthy, and both are asserted in
`tests/test_terminal_reason_coverage.py`:

- **Deduplicated by event id.** `add` writes every event to its index *and* its shard, so
  summing a union double-counts. Without deduplication the same evidence reports 2059
  bearing events instead of 2003, and the number moves whenever a shard is pruned.
- **Read leniently.** Events that today's validator would reject are still records of
  something that was observed. `ndjson.read_events` aborts a whole file on the first such
  line; the reporter does not, and instead counts unparseable lines and reports them.

## Do not close this gap by writing to the ledger

The reporter emits nothing. There is no backfill option and no synthesis option, and the
rendered report says so in its own output.

An audit ledger's only value is that its contents were observed. Writing four
`dispatch.exit` events to make the coverage table green would convert a true statement
about the fleet ("nothing has ever reported a usage limit") into a false one, destroy the
distinction the `stream_class` field exists to preserve, and leave a permanent record that
cannot be distinguished from evidence. The honest resolutions are to find or fix the
publisher that should emit the code, or to retire the code from the vocabulary.

Tests use fixture stores under `tmp_path` only. A test that resolves to the live index is
the defect agentops `b55df7e` fixed after two suites had already deposited ten fixture
events into a production ledger.
