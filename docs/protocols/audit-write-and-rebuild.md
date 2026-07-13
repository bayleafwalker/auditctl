---
doc_id: auditctl.audit-write-and-rebuild
status: draft
supersedes: null
---

# Audit write and rebuild protocol

## Boundary

One audit event has a stable ID and two representations:

- a SQLite row used as the local query index;
- one canonical line in a daily NDJSON shard used as the portable recovery and cockpit artifact.

Neither store alone is a distributed transaction coordinator. The command controls ordering and exposes recovery.

## Add operation

1. Validate and assign the event ID.
2. Begin an immediate SQLite transaction and insert the row.
3. Lock the shard, append one canonical line, and `fsync` it.
4. Commit SQLite.
5. Return success only after both writes complete.

## Effects and unknown outcomes

| Interruption | Possible durable state | Recovery |
|---|---|---|
| Before NDJSON append | Neither copy after SQLite rollback | Retry with a new event only when caller knows no success occurred |
| During append | SQLite rolls back; shard may require corruption detection | Inspect/repair shard before retry |
| After NDJSON fsync, before SQLite commit | NDJSON may contain the event; SQLite may not | Rebuild/import by stable event ID |
| After SQLite commit, before response | Both copies may exist; caller sees unknown outcome | Search by event ID or semantic ref before retry |

Cross-store atomicity is not claimed. A successful response promises both copies. Crash recovery promises detectable divergence and convergence from valid NDJSON shards.

## Concurrent writers

Shard append uses an exclusive advisory file lock, append mode, one encoded line, and `fsync`. The intended safety property is that completed concurrent writes produce complete, separately parseable lines. This depends on all writers using the auditctl append path and a filesystem honoring the locking assumptions.

## Rebuild

Rebuild validates all input events and imports by stable event ID. Duplicate IDs are skipped, making repeated import idempotent. `--replace` backs up the current database before importing; it does not rewrite source shards.

## Safety properties

- Successful `add` returns only after SQLite commit and NDJSON fsync.
- Reported NDJSON append failure does not leave a committed SQLite row.
- Concurrent completed append operations do not interleave JSON lines.
- Valid duplicate IDs do not create duplicate SQLite rows during rebuild.
- Invalid JSON or invalid event shape fails visibly with a shard and line reference.

## Liveness

- No progress guarantee is made if filesystem locking, disk space, or SQLite availability fails.
- Rebuild is operator-driven.
- A publisher retry is not exactly-once unless it preserves and reconciles the original event ID.

## Evidence

Reusable test intent lives in `verification/contexts/sqlite-ndjson-convergence.json`. Result packets must distinguish example, concurrency, and fault-injection evidence from documented-only claims.
