---
doc_id: auditctl.publisher-subprocess
contract_version: 2
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

## Actionq-daemon mapping

This section freezes the audit boundary for the future actionq daemon; it does
not claim the daemon or its caller has shipped. Actionq #973 owns that caller
after the daemon minimum and session lifecycle exist.

`source` is `actionq-daemon`. `actor` is `actionq:<runtime_session_id>` once a
session exists and `actionq:daemon` before it does. Every session event carries
both `session_id` (the actionq domain identifier) and `runtime_session_id` (the
shared observation-envelope field) with the same value. Required common
metadata is:

- `action_id`;
- `session_id` and `runtime_session_id` when a session exists;
- selected `harness` and `model` when known;
- `audit_status`/`audit_error` are recorded in the actionq lifecycle payload,
  not recursively inside a failed audit event.

`wi:<work_item_id>` is emitted only when the action target is a normalized
work-item reference. `sprint:<sprint_id>` is included when sprint context is
known. Action and session IDs are metadata, not invented ref prefixes. PR
events include `pr:<number>` and may include `sha:<commit>` when verified.

| Committed actionq fact | Audit type | Additional metadata |
|---|---|---|
| Action claimed and routed | `dispatch.queued` | routing source |
| Worktree ready and harness selected | `dispatch.started` | worktree, branch |
| Harness child PID exists | `session.start` | child PID |
| Pause/handoff recorded | `session.pause` | pause reason, handoff pointer when present |
| Session resumed from handoff | `session.resume` | prior session/handoff pointer |
| Child exit and validation outcome known | `session.exit` | outcome, result, failure reason, validation summary |
| Completed-session branch has an open PR | `pr.open` | PR number, state, branch |
| PR state is verified merged | `pr.merge` | PR number, state, branch, merge commit when known |

All emissions follow the source commit: an audit row never makes a queue,
lease, session, validation, or PR outcome authoritative. By default
`fail_action_on_emit_error=false`; failures remain visible in actionq lifecycle
payloads and logs but do not overwrite the original action outcome. The first
devbox rollout must not enable strict failure. A future strict profile may
reject before model work only when `dispatch.queued` or `session.start` cannot
be audited; it still must record the actionq outcome truthfully.

The caller performs no blind retry. Before retrying, it reconciles by
`source=actionq-daemon`, audit type, `action_id`, and `runtime_session_id` (plus
PR number for PR facts). Auditctl still generates the event and origin IDs.
The caller may then retry a missing observation, but this bounded reconciliation
is not an exactly-once guarantee.

## Session mechanization (Tier-0) mapping

This section freezes the audit boundary for the Tier-0 session wrapper
described in `agentops/docs/plans/agentops/session-mechanization-plan.md`. It
does not claim the wrapper has shipped; actionq owns that mechanism per the
plan's ownership section. Auditctl's role is limited to accepting and
round-tripping the observation types below — it does not interpret session
liveness, store raw prompts/transcripts, or mutate sprint state.

`source` is `session-wrapper`. `actor` is the wrapper's process identity.
Every event carries `runtime_session_id` in metadata (mapped by
`with_observation_envelope` onto the shared observation-envelope field), so
all events from one session correlate without needing a separate lookup.
`session.started` and both session-end types additionally carry
`origin_stream_id` as the producer outbox stream identity once enveloped;
publishers do not construct it.

| Committed wrapper fact | Audit type | Required refs | Required metadata |
|---|---|---|---|
| Session began | `session.started` | none required; `wi:<id>` or `sprint:<id>` only when Tier-1 rank is `explicit` | `runtime_session_id`, `repo_project`, `harness` (`model` is `null` for `harness=manual`) |
| Clean session end observed | `session.ended` | same as `session.started` | `runtime_session_id`, `end_reason` |
| No clean end observed (crash recovery) | `session.end-inferred` | same as `session.started` | `runtime_session_id`, `end_reason` |
| Session capsule finalized | `session.capsule-pointer` | `capsule:<capsule_id>` | `runtime_session_id`, `capsule_id` |

`session.capsule-pointer` is non-validation-bearing: it makes a finalized
`session-capsule/v1` artifact discoverable to the periodic scribe and
post-session reconciler (`session-mechanization-contracts.md`); auditctl does
not validate the referenced artifact's contents. `session.ended` and
`session.end-inferred` are mutually exclusive per session — the wrapper picks
one, matching `end.kind` in the capsule contract (`clean-end` vs
`end-inferred`).

As with the sprintctl and actionq mappings, required-metadata columns are a
publisher contract, not a per-event-type schema enforced inside
`validate_event_object` — auditctl validates the shared observation envelope
and typed-ref grammar for every event type uniformly and leaves domain-shaped
metadata requirements to the publisher and its own tests.

## Actionq review-result mapping

This section versions the publisher boundary used to record an independently
completed candidate or integration review. It does not make auditctl an
approval authority and does not move review findings into the audit ledger.

The event type is `candidate.reviewed`, `source` is `actionq-review`, and the
top-level `actor` is the authenticated review identity that performed the
review. The event always has `sha:<reviewed-git-commit>` as a ref. It also has
the applicable `wi:<work_item_id>` and `sprint:<sprint_id>` refs when those
contexts are present and valid. Publishers must not invent placeholder work or
sprint refs when the context is absent.

Required metadata is exactly:

- `action_id`;
- `attempt_id`;
- `plan_ref`;
- `subject_kind`, either `candidate` or `integration`;
- `publication_ref`;
- `verification_result_ref`;
- `review_result_artifact_ref`;
- `topology`;
- `findings_digest`;
- `review_outcome`, either `no-findings` or `findings-recorded`;
- `runtime_session_id` when a runtime session exists.

The metadata has no approval, acceptance, merge, or release field. Full review
findings remain in the immutable external artifact named by
`review_result_artifact_ref`; the audit event contains only its reference and
`findings_digest`. Summary and detail must remain bounded and redacted and must
not reproduce findings, credentials, receipts, prompts, transcripts, or raw
runner output.

Emission follows creation of the immutable review-result artifact. A missing
binary, timeout, signal, or nonzero auditctl exit does not alter that result.
The publisher performs no blind retry because each `auditctl add` invocation
mints a new event ID. Before retrying it reconciles by `source=actionq-review`,
type `candidate.reviewed`, `action_id`, `attempt_id`, `plan_ref`,
`subject_kind`, `publication_ref`, `verification_result_ref`, and
`review_result_artifact_ref`. It retries only when that observation is absent.
This makes publisher retry idempotent at the reconciliation boundary; it does
not make auditctl insertion exactly once.

## Evidence and compatibility

Sprintctl's current source and unit histories are pinned on auditctl work item
#964. `sprintctl/tests/test_audit_events.py` verifies mappings, source-success
ordering, failure degradation, and an actual fake-binary `PATH` invocation.
Auditctl tests verify that the resulting `add` argv is accepted and produces a
schema-valid observation envelope.

The actionq contract and clean daemon-plan revision are pinned on auditctl work
item #965. Auditctl's actionq-shaped fixture validates the complete subprocess
argv and resulting envelope now; actionq #973 must add fake-client call-site
tests when the daemon implementation lands.

The session mechanization (Tier-0) mapping above is pinned on auditctl work
item #1113. Auditctl's session-wrapper-shaped fixtures validate the complete
subprocess argv and resulting envelope, shard append, and rebuild round-trip
for `session.started`, `session.ended`, `session.end-inferred`, and
`session.capsule-pointer` now; the wrapper caller itself (actionq-owned) and
its own call-site tests are out of scope for this item.

Adding an event type or changing required metadata is a versioned contract
change. Version 2 adds `candidate.reviewed`. Existing event types, typed refs,
and the warn-only failure posture remain backward-compatible requirements from
version 1.
