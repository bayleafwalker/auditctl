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

1. Validate and assign the stable audit event ID.
2. Begin an immediate SQLite transaction and reconcile valid NDJSON shards.
   This recovers any record fsynced by a writer that crashed before its SQLite
   commit, including that record's producer cursor.
3. Begin the event transaction, mint or load the durable
   `origin_stream_id`, and allocate the next `origin_seq` in that transaction.
4. Insert the observation envelope into SQLite.
5. Lock the shard, append one canonical line, and `fsync` it.
6. Commit SQLite.
7. Return success only after both writes complete.

New records retain `id` as auditctl's stable event identity and also carry the
shared producer envelope: matching `event_id`, schema version, observation
class, origin stream and sequence, mapped event type and occurrence time,
optional runtime/basis/correlation fields, and a digest of the canonical audit
payload. Legacy records without these additive fields remain valid and
rebuildable.

## Effects and unknown outcomes

| Interruption | Possible durable state | Recovery |
|---|---|---|
| Before NDJSON append | Neither copy after SQLite rollback | Retry with a new event only when caller knows no success occurred |
| During append | SQLite rolls back; shard may require corruption detection | Inspect/repair shard before retry |
| After NDJSON fsync, before SQLite commit | NDJSON may contain the event; SQLite may not | Rebuild/import by stable event ID |
| After SQLite commit, before response | Both copies may exist; caller sees unknown outcome | Search by event ID or semantic ref before retry |

Cross-store atomicity is not claimed. A successful response promises both copies. Crash recovery promises detectable divergence and convergence from valid NDJSON shards.

Reconciliation and event insertion are separate SQLite transactions. This is
intentional: the first transaction repairs NDJSON-ahead state; the second
allocates and writes the new observation. Another conforming writer may run
between them and consume the next sequence, but every committed envelope still
has one monotonically increasing sequence in the shared producer stream.

## Producer identity and sequence

One audit ledger mints one UUID `origin_stream_id`. Its next sequence is stored
in SQLite and updated under `BEGIN IMMEDIATE`. A reported append failure rolls
back both the event row and its allocation, so the unused sequence can be
retried. If the line reached `fsync` before a crash, the next writer reads that
line while holding the SQLite writer lock, imports it, advances the cursor, and
only then allocates. This prevents a restart from assigning the same origin
tuple to a different event.

The gap-free claim is bounded to valid records produced by one audit ledger and
all writers using this add/rebuild path. Corrupt or missing shards stop
reconciliation visibly; auditctl does not skip them or fabricate cursor
progress. Remote ingestion offsets and gap arbitration remain outside
auditctl.

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
- New observations have one durable stream identity and unique, gap-free local sequences.
- Restart after an NDJSON-ahead crash reconciles the old sequence before allocation.

## Liveness

- No progress guarantee is made if filesystem locking, disk space, or SQLite availability fails.
- Rebuild is operator-driven.
- A publisher retry is not exactly-once unless it preserves and reconciles the original event ID.

## Evidence

Reusable test intent lives in `verification/contexts/sqlite-ndjson-convergence.json`. Result packets must distinguish example, concurrency, and fault-injection evidence from documented-only claims.
