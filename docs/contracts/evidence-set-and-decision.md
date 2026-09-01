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

**`Decision`: yes, by owner ruling of 2026-09-01.**

This section previously said no, and the reason was in this repository's own code:
`record_class` was hard-validated to the single value `observation`, and
`AuditContext.as_record` states the rule it enforced — *"auditctl records
conformance, it does not state desired state."* A `Decision` is a judgement about
what should follow from evidence, which is desired state by definition. Admitting
it meant relaxing the one constraint keeping this store a record of what happened,
so it was an owner decision about the boundary rather than a schema change.

The owner took it. `decision` is now admitted, and the settlement spine has one
home: EvidenceSet as an observation, Decision as a judgement, in the same store.

What survives the ruling is the *reason* the constraint existed. Mixing the two
irreversibly was the failure the single value prevented; being unable to tell them
apart is what would actually cost something. So:

- The vocabulary stays **closed** (`observation`, `decision`). An unlisted class is
  still refused, in both the local validator and central ingestion, and by a
  database CHECK. An open column would give the failure back by accident.
- `record_class` is a **queryable column**, not a payload field, so a reader can
  separate what happened from what someone concluded about it.
- It is part of each record's **immutable hash**. Two records identical but for
  their class produce different digests, so the layer whose job is telling records
  apart cannot conflate them. Observations hash to exactly what they always did,
  so no existing digest changed.
- Nothing infers the class. A caller recording a judgement passes `decision`
  deliberately; guessing it from event type or payload shape would reintroduce the
  ambiguity the closed vocabulary exists to prevent.

Central migration 3 widens the CHECK. It finds the old constraint by catalog lookup
rather than by its assumed default name: dropping a wrong name with `IF EXISTS`
would leave the original single-value constraint in force while adding the widened
one beside it, so decisions would still be rejected and the migration would report
success. It raises instead. Every existing row is `observation` and satisfies the
widened constraint, so no row is rewritten and no backfill is required.

Acceptance Lab remains the natural *producer* of terminal decisions. This is where
they are durably recorded, not where they are made.

## Why this is not deferral

The register's item asked for "an adapter or an explicit supersession". This is
the adapter, and there is no supersession, because neither definition is wrong.
What was missing was never a third implementation; it was a written statement of
how the two relate and where the boundary falls. Adding a third `EvidenceSet` to
this repository would have created the drift the register exists to remove.
