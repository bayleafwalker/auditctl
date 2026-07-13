# auditctl state-protocol overlay

## Closed subjects

| Subject | State owner | Default depth | Primary anchors |
|---|---|---:|---|
| Audit event identity | Event ID in both stores | 1 | `auditctl.ids:new_event_id`, validation |
| SQLite + NDJSON write | CLI sequencing plus two stores | 2 | `auditctl.cli:add_cmd`, `auditctl.ndjson:append_event` |
| Concurrent shard append | NDJSON file lock | 2 | `auditctl.ndjson:append_event` |
| Rebuild convergence | NDJSON shards keyed by event ID | 1 | `auditctl.cli:rebuild_cmd`, `auditctl.db:import_events` |

## Required scenarios

- Concurrent processes append complete, parseable lines without interleaving.
- NDJSON append failure leaves no committed SQLite row.
- Crash after NDJSON fsync but before SQLite commit leaves NDJSON ahead and rebuild converges.
- SQLite commit failure after append is visible and recoverable.
- Duplicate event IDs across one or more shards import exactly once.
- Corrupt or partial final lines fail visibly instead of being silently skipped.
- Rebuilding twice is idempotent.

## Consistency language

Do not claim cross-store atomicity or linearizability. The supported contract is:

- both copies exist after a successful `add` response;
- SQLite does not commit when NDJSON append reports failure;
- some crashes may leave NDJSON ahead;
- stable event IDs and rebuild provide convergence.

Use a real filesystem and separate processes for Depth 2. Monkeypatch-only tests do not establish file-lock or crash semantics.
