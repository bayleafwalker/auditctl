# EvidenceSet and Decision: what auditctl holds, and what it does not

Status: contract, 2026-09-01. Resolves the open question left by vuoro `9886130`
("settle auditctl as the home of EvidenceSet and Decision"), which changed
documentation only and left no definition behind.

## The two EvidenceSets are not rivals

Two things named `EvidenceSet` exist in the portfolio. The disposition register
recorded them as "structurally unrelated, sharing only a name, with no adapter
and no supersession", which is accurate as a description of the code and
misleading as a diagnosis. They are not competing definitions of one object.
They are a container and one of the things it can contain.

`bindery-core:pkg/evidencev1` answers **did independent accounts of one
execution agree?** It takes two or more `ObservationSummary` values from
distinct observers, compares them by a declared `Method` (`exact-count`,
`ordered-hash`, `semantic-equivalence`, `quorum`, `domain-specific`), and
returns `consistent` or `inconsistent`. It refuses a summary that will not say
whether it was `client-reported` or `broker-derived`, because a producer's
account of itself is not independent evidence. Its identity is a sha256 digest
over `(execution_id, method, observations)`.

`vuoro:packages/vuoro-evidence` answers **is there enough valid evidence to act
without observing again?** An `EvidenceSet` is a bundle of `EvidenceItem`s and
`EffectGrant`s; a reducer consumes it and emits a `Decision` of `accept`,
`reacquire`, `reconcile` or `reject`.

One is an agreement test. The other is a sufficiency test. A reconciliation
result is an *input* to a sufficiency judgement, never a substitute for it.

## The adapter

A bindery `evidencev1.EvidenceSet` maps onto exactly one vuoro `EvidenceItem`:

| `EvidenceItem` field | from the bindery record |
| --- | --- |
| `item_id` | `evidence_set_id` |
| `kind` | `bindery.evidencev1.reconciliation` |
| `ref` | `evidence_set_id` (it is already the content address) |
| `digest` | `evidence_set_id` — a `sha256:<64 hex>` digest over the identity tuple |
| `collector` | the reconciling broker's observer id |
| `validity` | `basis: indefinite` — content-addressed, so valid at its digest forever |
| `claims` | one `Claim` of `claim_type: observation`, `subject: execution_id`, `confirms: true` when `reconciliation.outcome == "consistent"`, `false` when `"inconsistent"` |
| `provenance` | `method`, `compared_observers`, `distinct_counts`, `distinct_hashes`, and each summary's `source` |

The `source` field must survive the mapping. An evidence set whose members are
all `client-reported` establishes agreement between accounts that were never
independent, and a consumer that cannot see this will read a `consistent`
outcome as corroboration when it is only consistency.

`confirms` is deliberately not set from agreement alone: equal counts do not
prove semantic identity, which the Go package says of its own policy #1. A
`consistent` outcome confirms *the reconciliation claim*, not the underlying
effect.

## What auditctl holds

**`EvidenceSet`: yes, as an observation.** Evidence is observed, so it fits the
store's existing `record_class: observation` without widening anything.

**`Decision`: no, and the settlement commit was wrong to place it here.** The
reason is in this repository's own code, not in a preference. `record_class` is
hard-validated to the single value `observation`
(`auditctl/validation.py:372`), and `AuditContext.as_record` states the rule it
enforces: *"auditctl records conformance, it does not state desired state."* A
`Decision` is a judgement about what should follow from evidence. It is the
canonical example of desired state.

Admitting `Decision` here would mean relaxing `record_class`, which is the one
constraint keeping this store a record of what happened rather than a record of
what someone concluded. That is an owner decision about the boundary, not a
schema change, and it is deliberately not taken here.

The consequence is small and specific: `Decision` needs a home that is allowed
to hold judgements. Acceptance Lab already emits terminal decisions and already
has the evaluator that produces them. Auditctl then holds the observation that
a decision was recorded — an event about the judgement — without holding the
judgement itself.

## Why this is not deferral

The register's item asked for "an adapter or an explicit supersession". This is
the adapter, and there is no supersession, because neither definition is wrong.
What was missing was never a third implementation; it was a written statement of
how the two relate and where the boundary falls. Adding a third `EvidenceSet` to
this repository would have created the drift the register exists to remove.
