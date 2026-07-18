---
doc_id: auditctl.publisher-subprocess
status: adopted
supersedes: null
---

# Publisher subprocess contract

Audit publishers invoke the `auditctl` executable. They do not import auditctl
as a Python library and do not write its SQLite database or NDJSON shards
directly. Audit observations are integration side effects; each source domain
commits its own authoritative operation before invoking this boundary.

## Common invocation

```text
auditctl add \
  --type <allowed-event-type> \
  --source <publisher-source> \
  --actor <publisher-process-actor> \
  --summary <bounded-summary> \
  [--detail <bounded-detail>] \
  [--ref <typed-ref>]... \
  --metadata <json-object>
```

Arguments are passed as an argv vector, never through a shell. Metadata is one
JSON object. Repeated `--ref` arguments use auditctl's typed ref grammar. A
successful invocation receives auditctl's generated stable event ID and
producer origin tuple; publishers must not construct either store directly.

The top-level audit actor identifies the process identity that published the
observation. When a domain operation has a distinct actor, the publisher also
records that domain actor in metadata. This preserves who invoked auditctl and
who performed the source operation without conflating them.

## Sprintctl mapping

`source` is always `sprintctl`. The allowed initial mappings are:

| Committed sprintctl fact | Audit type | Required refs | Required metadata |
|---|---|---|---|
| Active sprint created or planned sprint activated | `sprint.opened` | `sprint:<sprint_id>` | `sprint_id`, `event_type=sprint-opened` |
| Sprint close boundary committed | `sprint.closed` | `sprint:<sprint_id>` | `sprint_id`, `event_type=sprint-closed`, `boundary_event_id`, `boundary_revision`, domain `actor` |
| Takeup event committed | `sprint.taken_up` | `sprint:<sprint_id>` | `sprint_id`, `event_type=sprint-taken-up`, domain `actor` |
| Release event committed | `sprint.released` | `sprint:<sprint_id>` | `sprint_id`, `event_type=sprint-released`, domain `actor` |
| Knowledge-class item note committed | `knowledge.landed` | `sprint:<sprint_id>`, `ka:<knowledge_event_id>` | `sprint_id`, `event_type=knowledge-landed`, `knowledge_event_id`, `note_type` |

Failed source operations emit nothing. Non-knowledge notes emit nothing. The
publisher invokes auditctl only after the sprintctl database operation has
succeeded.

## Failure, timeout, retry, and duplicates

Sprintctl bounds the subprocess call with a 10-second timeout. A missing
binary, timeout, signal, or nonzero exit produces a warning on stderr and does
not reverse or fail the already-committed sprintctl operation. Auditctl is not
the authority for sprint state.

Sprintctl performs no automatic retry in this contract. `auditctl add` mints a
new event ID for each invocation, so a blind caller retry can create two valid
observations for one source fact. An operator or future reconciler must first
search by stable typed refs plus source metadata and retry only when the fact is
absent. No exactly-once or cross-process transaction claim is made.

Auditctl rejects malformed refs or metadata and preserves its own dual-write
recovery rules. Publishers may log the warning, but must not bypass the CLI by
writing one audit store directly.

## Evidence and compatibility

Sprintctl's current source and unit histories are pinned on auditctl work item
#964. `sprintctl/tests/test_audit_events.py` verifies mappings, source-success
ordering, failure degradation, and an actual fake-binary `PATH` invocation.
Auditctl tests verify that the resulting `add` argv is accepted and produces a
schema-valid observation envelope.

Adding an event type or changing required metadata is a versioned contract
change. Existing event types, typed refs, and the warn-only failure posture are
backward-compatible requirements for the v1 publishers.
