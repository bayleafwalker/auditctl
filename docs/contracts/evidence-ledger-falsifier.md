# The evidence-ledger falsifier

Status: contract, 2026-09-12. Implements the falsifier bound to the 2026-08-30 owner
settlement recorded in `vuoro:docs/plans/2026-08-22-long-term-direction.md` section 14.

## The settlement and its falsifier

The settlement, quoted:

> **Auditctl is the canonical home of `EvidenceSet` and `Decision`.** `[settled 2026-08-30
> by the owner]` [...] The consequence is the reason it was asked. Every artifact that
> today improvises whether it is a record or a build output — `verification/results/`,
> acceptance-lab `campaigns/` — is a **derived build output that cites a ledger id**, and
> the gate guarding each stops having to invent which it is. Falsifier: an evidence
> artifact that no ledger id resolves to, or a decision reachable only by reading a file
> in a working tree.

The local disposition is in
[`evidence-set-and-decision.md`](evidence-set-and-decision.md): `EvidenceSet` as an
observation, `Decision` as a judgement, both in this store, `record_class` keeping them
apart.

What was missing was the falsifier itself. A settlement bound to a falsifier that nobody
can run is a settlement bound to nothing.

## What the check does

```bash
auditctl check evidence-ledger --root <tree> [--index <db>] [--shards <path|glob>]
auditctl check evidence-ledger --root <tree> --json --report-only
```

It classifies files as evidence, extracts the ledger ids they cite, resolves those ids
against real stores, and reports every artifact for which no id resolves. It exits
non-zero when the falsifier fires; `--report-only` reports without gating.

**Evidence artifacts are declared, not guessed.** Guessing would reintroduce the
improvisation the settlement removes.

- any file under a `verification/results/` directory;
- any file under a `campaigns/<name>/` directory;
- any JSON file whose top-level `schema_version` names a known evidence schema
  (`verification-result/`, `evidence-set/`, `decision/`).

The directory shapes are matched against the whole path, not the part below the scan root.
Matching the remainder made the verdict depend on where the caller pointed: scanning
`acceptance-lab` classified the campaign artifacts and scanning `acceptance-lab/campaigns`
classified none of them, because the directory that names them had been consumed by the
root. That mattered in practice — the real campaign files declare `schema_version: 1`,
which names no schema, so the directory is the only signal there is.

**A ledger id is an auditctl event id**: `ad:` followed by 26 Crockford base32 characters,
the form `validation.EVENT_ID_RE` accepts. An artifact cites one by carrying it anywhere
in its bytes — a `ledger_id` field, a `refs` list, or prose. Liberal about where the
citation sits, strict about whether it resolves: no field name for the citation has been
agreed, and requiring one would fail every artifact for a reason the settlement does not
state.

**Three reasons, closed**, so a report cannot invent a fourth:

| reason | meaning |
| --- | --- |
| `no_ledger_id` | the artifact cites nothing |
| `unresolvable_ledger_id` | it cites a well-formed id that names no event |
| `decision_only_in_working_tree` | it states a judgement, and no ledger *decision* record resolves it |

`unresolvable_ledger_id` is the worse of the first two, not the better one: it reads as
compliant and is not.

## Measured, 2026-09-12

Run against every `verification/results/` and `campaigns/` directory on this workstation,
with citations resolved against a ledger of **3861 events across 60 stores** (10
per-repository sqlite indexes plus the workspace index, and 49 NDJSON shards; zero read
failures):

| tree | evidence artifacts | offenders |
| --- | --- | --- |
| `auditctl/verification/results` | 7 | 7 |
| `actionq/verification/results` | 7 | 7 |
| `sprintctl/verification/results` | 15 | 15 |
| `vuoro/verification/results` | 3 | 3 |
| `acceptance-lab/campaigns` | 14 | 14 |
| **total** | **46** | **46** |

All 46 under `no_ledger_id`. Not one evidence artifact in the workspace carries a ledger
id of any kind, well-formed or dangling.

**The falsifier fires. That is the finding, not a defect in the check.** The convention
post-dates every file in these directories, so the result was the expected one; what the
check adds is that it is now measured, listed by path, and re-runnable rather than
asserted.

Two numbers deserve reading carefully rather than at face value:

- `unresolvable_ledger_id` is **0** because nothing cites anything at all, not because
  citations are healthy. The moment one artifact starts citing, this is the count to
  watch.
- `decision_only_in_working_tree` is **0** for a sharper reason: the ledger holds **zero**
  `record_class: decision` records across all 60 stores, so every judgement in these
  trees — every `status: PASS`, every `claims` array — is reachable only by reading a file
  in a working tree. The first limb catches those files before the second limb is
  reached, so the second limb's zero is an artifact of ordering. Both limbs of the
  settled falsifier are currently violated by the same 46 files.

## It never repairs

There is no `--fix`, no id minting, and no write path in this module. The rendered report
says so in its own output.

Stamping a ledger id into an artifact would satisfy the text of the falsifier while
destroying the property it protects: the id would resolve to nothing — which this check
reports as the worse shape — or it would resolve to an event invented to make it resolve,
at which point the store has stopped being evidence of anything.

The remedy is the one the settlement describes. Record each artifact's evidence in the
ledger, and have the producing build cite the id it received. The artifact then becomes
what the settlement says it already is: a derived build output, safe to regenerate or
prune, because the record it cites lives somewhere that is not a working tree.

## Tests

`tests/test_evidence_falsifier.py` proves both shapes on fixture trees under `tmp_path` —
the failing shape and the passing shape — because a check that only ever fires cannot be
told from a broken one. No test reads or writes a live store; that is the defect agentops
`b55df7e` fixed after two suites had already deposited fixture events into a production
ledger.
