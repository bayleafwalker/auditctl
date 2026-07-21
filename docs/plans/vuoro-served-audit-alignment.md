---
doc_id: auditctl-vuoro-served-audit-alignment
status: ratified
ratified_at: 2026-07-21
ratified_by: operator
governing_decision: agentops/docs/plans/agentops/vuoro-served-substrate-plan.md
---

# Auditctl alignment with the Vuoro audit module

Auditctl remains durable machine-local capture: SQLite is the query index and
NDJSON is the portable recovery record. The Vuoro audit module accepts
idempotent observation submission and returns receipts. It does not replace
local buffering/rebuild or turn audit events into authority transitions.

## Required changes

- Define remote observation-ingest and receipt handlers using the existing
  `origin_stream_id` and `origin_seq` contract.
- Register submit, receipt lookup, and bounded read operations in the Vuoro
  catalog.
- Add a central audit ingest schema and deployment migration entrypoint with
  separate migration/runtime roles.
- Preserve append-then-submit behaviour, duplicate detection, gap visibility,
  and lost-response recovery.
- Publish local-effect and session evidence without granting auditctl sprint,
  queue, knowledge, or acceptance authority.

`vuoro-dev` acceptance covers offline append, delayed upload, duplicate retry,
missing sequence, lost response, receipt lookup, rebuild, and service restart.
The local shard remains sufficient to retry or reconstruct after remote loss.
