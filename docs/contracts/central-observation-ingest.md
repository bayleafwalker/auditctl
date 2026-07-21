---
doc_id: auditctl-central-observation-ingest
status: ratified
governing_decision: auditctl-vuoro-served-audit-alignment@git:0751c6a77cacc5a2dd016705a5d2e9756a3216f2
---

# Central observation ingest contract

## Boundary

The central audit schema is an observation ledger and receipt index for a
served Vuoro audit adapter. It accepts only the existing auditctl
`record_class=observation` envelope. It has no tables or operations for claims,
grants, queue leases, knowledge acceptance, authority commands, or remote
decisions.

SQLite remains the machine-local query index and NDJSON remains the portable
producer/recovery record. A successful local append does not depend on the
central database. If submission or its response is lost, the original NDJSON
record is the retry source; central loss can be reconstructed from those
records.

The HTTP/catalog adapter is a separate change. It must call this owner-provided
application core rather than duplicate admission rules in Vuoro.

## Admission and receipt identity

Each observation is validated with auditctl's local envelope validator, then
canonically hashed over its producer identity, event identity, observation
metadata, payload, and producer timestamp.

For one `origin_stream_id`:

1. The runtime inserts or locks the stream cursor row.
2. The runtime takes a transaction advisory lock for the environment schema
   and global `event_id`, so different origin streams cannot race the event-ID
   precheck. The exact database event-ID constraint is also translated to the
   owner-level conflict error as a defensive fallback.
3. An exact retry of an existing `(origin_stream_id, origin_seq)` and record
   hash returns the original `receipt_id` and increments duplicate telemetry.
4. Reusing the tuple for different content is a conflict.
5. Reusing an `event_id` for another origin record is a conflict; the losing
   stream transaction leaves neither a stream row nor an observation.
6. A new observation must equal `highest_contiguous_seq + 1`.
7. A higher sequence is rejected without partial admission and reports the
   expected and received sequence.
8. The observation, receipt, and cursor advance commit in one PostgreSQL
   transaction.

The stream row lock serializes admission for one producer stream. Different
streams and different environment schemas remain independent. A caller whose
response is interrupted has an unknown response outcome, but retrying the same
record yields the stable receipt rather than a second observation.

## Read contract

Reads are ordered by the server-assigned `ingest_offset` and require a limit
between 1 and 100. Indexes support offset, event-type, origin-stream, and
occurrence-time access. Receipt lookup accepts exactly one stable receipt ID or
event ID. Stream status exposes `highest_contiguous_seq` and
`next_expected_seq`; it does not invent or fill missing producer events.

## Compatibility and roles

The current central schema is version 2 and serves `audit/v1`. Runtime
compatibility requires:

- an installed schema within the package's explicit supported range;
- the connection's `current_user` to match the recorded runtime role; and
- the runtime principal record written by the migration entrypoint.

Compatibility checks are read-only. An absent, older, newer, or role-mismatched
schema fails closed and is never migrated during service startup. A principal
without schema usage receives an explicit `schema_access_denied` state without
attempting a table read.

The deployment migration role owns and applies DDL. The runtime role receives
only schema usage, bounded table DML, sequence usage, and compatibility reads.
It receives no schema `CREATE`, table `DELETE`, migration-ledger write, or
authority-table privileges. Conversely, the migration role fails the runtime
compatibility gate even though it owns schema objects, so it cannot be used to
serve normal requests through the conforming adapter. When deployment rotates
the configured runtime role, the migration transaction revokes all schema,
table, and sequence privileges from the previous principal before granting the
new one.

## Established and deferred evidence

Disposable-PostgreSQL tests establish within their stated bounds:

- empty-to-v1-to-v2 upgrade with receipt backfill;
- idempotent and advisory-lock-serialized migration jobs;
- checksum drift and newer-schema refusal;
- duplicate, lost-response retry, conflict, gap, and bounded-read behaviour;
- concurrent identical submission returning one receipt;
- separate schemas retaining isolated histories; and
- runtime DDL denial, runtime-role rotation, and migration-role service refusal.

Deployment topology, identity issuance, Vuoro catalog publication, HTTP fault
injection, backup/restore, and cross-machine functional tests remain owned by
the later adapter and appservice rollout work.
