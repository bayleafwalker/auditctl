# EC-07 Auditctl source inventory

This is pre-foundation discovery for auditctl#2740, not a new interface contract
or migration acceptance. It captures Auditctl source
`1a76e72da79fffd7d2a18ec38c9e06b950aff023` and three explicitly named committed
consumer edges. The [inventory](ec07-interface-inventory-20261018.json) records
source-byte hashes, five CLI commands, five existing served declarations and
eight boundaries with producer, consumer, semantic owner, entry point,
transport, request/result, errors, identity/scope, persistence, retry and gaps.
It is not an exhaustive estate consumer census or a deployed-catalog probe.

The governing [estate rewrite](https://github.com/bayleafwalker/agentops/blob/e61a1f10766912c67d490ce6f4a5e42bf4e31229/docs/plans/agentops/2026-10-18-estate-contract-rewrite.md)
requires EC-03 generation before EC-07 migration. EC-00's qualified ADR
(vuoro#2734) and the selected foundation must precede new interfaces. No IDL,
transport, package or version is selected here. Local capture, historical S4
source authority, explicit central administration and separately owned service
deployments retain their current boundaries. The existing
[KAOS roadmap](../plans/2026-10-18-kaos-roadmap.md) also retains provenance,
interrupted-import reconciliation and missing-source uncertainty.

## Existing boundaries and consumer evidence

| Boundary | Existing producer and consumer | Retained semantics and qualification limit |
|---|---|---|
| Publisher subprocess | Source domain commits, then calls `auditctl add`; Sprintctl `_emit_audit_event` and Agentops cost hooks are inspected committed source edges. | Generated event/origin identity; argv rather than shell; source result is independent of audit emission. A warning/timeout can leave an unknown audit outcome. No blind retry or exactly-once publication. Publisher mapping declarations for ActionQ/session/review are not proof those consumers are currently deployed. |
| Local append | `cli.add_cmd` uses the local SQLite producer ledger and locked NDJSON append/fsync. | A successful response establishes both copies. These are not one atomic transaction: NDJSON can be ahead after a crash or SQLite commit failure. Reconciliation precedes further sequence allocation. Local availability does not depend on central PostgreSQL or a service. |
| Local list/render | Explicit CLI reads the SQLite index. | The index is a derived local query view; a row count or empty result is not proof of historical completeness. Text/JSON/markdown are existing presentations, not generated cross-language contracts. |
| Snapshot attribution | Explicit frozen files and overlays go to the installed Agentops query helper through a bounded subprocess consumer. | `original_event` remains unchanged; attribution is a separate derived view, not producer identity or authorization. No index access, automatic tail discovery or S4 import follows. |
| Rebuild | Operator supplies NDJSON shards to the local index rebuilder. | Complete-input preflight precedes writes or replacement; exact canonical duplicate identity is idempotent, conflicting content/provenance fails. Original shard bytes are not rewritten. Supported legacy absence is not a general future decoder policy. |
| Frozen-source preflight | Reviewer supplies a manifest and separately recorded exact digest. | Explicit regular files, strict JSON, exact raw-line digests/offsets and named digest domains; missing/unknown domains remain partial. This does not establish Git provenance, the full estate inventory, destination admission or cutover. |
| Optional central runtime | Explicit `VuoroAuditAdapter` calls Auditctl-owned admission/read functions; Vuoro source composes that adapter. | Five protocol-v1 declarations; one submit authority and one read authority. Receipt/cursor transaction and stable exact retry do not accept work or grant effects. Service identity, tenant visibility, revocation and deployed compatibility require separate qualification. |
| Central administration | Explicit `auditctl-central-schema` entry point uses the migration owner. | Immutable SQL assets and separate runtime/migration roles; no startup or local CLI migration. Current schema source does not establish deployed migration, backup or recovery evidence. |

The inventory pins the committed Sprintctl publisher, Agentops hook and Vuoro
composition files separately. They establish source edges only: no live binary,
hook installation, issued grant, workload or successful service invocation was
observed. Source hashes establish byte equality, not authenticity or authority.
No runtime profile, credential, original workspace event or shard was read.

## Identity, bytes and digest domains

Keep these identities separate through generation and migration:

- A generated local `ad:` event ID and producer `(origin_stream_id, origin_seq)`
  identify observations. A new publisher invocation can mint another identity
  for the same source fact; transport request identity is not a replacement.
- Original NDJSON file/line bytes, their SHA-256 values and the frozen manifest
  digest bind source evidence. JSON display objects must not be reserialized
  and substituted for those originals.
- `canonical_payload_json` and `central.prepare_observation` define the current
  payload and `auditctl-central-v1` record digest domains. The central record
  selects defined envelope fields; it is not a digest of every unknown field
  or local provenance field. The local rebuild identity additionally preserves
  its persisted provenance. These deliberately different domains must not be
  collapsed or silently restamped.
- Observation versus decision is an explicit closed record class, included in
  the central record hash. Storing a judgment does not apply it to current work
  or move settlement/effect authority into Auditctl.
- An ingest receipt, source qualification report, derived attribution, target
  observation and authoritative work acceptance are different evidence.
  Correlation does not establish causal acceptance or comparable outcomes.

## Requirement reconciliation

Each acceptance clause remains evidence or a named gap, rather than inheriting
qualification from a source hash or a passing local test.

| Requirement | Existing evidence inspected | Remaining qualification |
|---|---|---|
| ER-021: no secrets/unauthorized workspace records in tool, UI or traces; deleted/revoked behavior | Snapshot reports omit raw payloads/paths where specified, and helper errors are not echoed. Publisher contracts require bounded/redacted summaries. Adapter metadata declares authorities. `test_attribution_listing.py` exercises bounded refusal and unchanged originals. | General secret-fixture coverage and actual composed tenant entitlements, tool/UI/trace redaction, deletion and revocation semantics were not established. Shape validation and catalog authority strings are not a general secret detector or authorization proof. |
| ER-022: cross-language map/numeric/unknown/schema vectors, changed meaning invalidates binding, original signed bytes retained | Sorted compact JSON central hashing, strict preflight JSON, record-class hashing and local provenance identity have Python examples in `test_central_contract.py`, `test_source_preflight.py` and `test_rebuild_provenance.py`. Raw-line digests remain separate from derived hashes. | No selected cross-language canonicalization/signature contract or Python/TS/Go/Rust golden-vector campaign. Unknown/numeric/schema behavior across generated projections and changed signed meaning require qualification. No claim that current records are universally signed; retain actual original bytes and known/unknown digest domains. |
| ER-023: retained historical decoders/bytes/digests and linked transformed views; no restamping | Rebuild explicitly accepts supported legacy envelope absence; source preflight preserves raw bytes/digest status and attribution wraps unchanged originals. Central supports its declared envelope version and closed record classes. | Full historical schema/decoder inventory and retained generated packages, cross-version compatibility and transformation provenance links remain future work. Current central projection is not a lossless archive of every unknown local field. |
| ER-024: expired/unauthorized references fail, corruption detected, bounded large-artifact control messages, portable reconstruction | Typed-ref grammar, explicit regular-file checks, manifest/line hashes, read limits and bounded helper spool are present. `test_source_preflight_bounds.py` pins manifest/record limits. | Typed refs and result-digest pairing do not dereference artifacts or prove existence, expiry or permission. Central row limits do not alone bound serialized bytes. Artifact-home authorization, corruption/large-reference transport, expired references and portable export reconstruction need owner integration evidence. |
| ER-029: missing observations unknown, comparable accepted-outcome cohorts, no router/profile recreation | Frozen-source and attribution reports name their limited source scope; original decisions remain judgments, not acceptance of current work. Existing metadata preserves correlation/provenance without model routing. | No claim of complete observation coverage, causal accepted-outcome joins, comparable cohorts, cost, review effort or operator attention measurements. These require their owning work/learning and real-use evidence; empty local results are not completeness. No model router or compiled profile is introduced. |
| ER-042: separately justified/tested storage migration, retained backup schemas/keys, restored meaning | Existing local rebuild/replace and explicit central checksum migration/role contracts are inspected. Retained prior result packets describe bounded recovery/admission tests. | This inventory justifies no new storage choice or migration. Actual backup/schema/key policy, whole-system restore, older decoder availability, consumer cutover and rollback of newly written records need deployment/owner evidence. No keys or live backup data were accessed. |

## Verification and limits

The existing source suite passed **235 tests, 11 skipped in 10.59 seconds** using
`uv run --frozen pytest -q`, with
`AUDITCTL_TEST_ATTRIBUTION_QUERY` naming the exact reviewed Agentops helper
extracted from commit `7b5c02a14bee88e7c19ed81f4f510800c0160a8c`. All test
writes used temporary repositories/artifact roots. The first attempt omitted
that required external fixture and produced 34 setup errors; supplying the
reviewed helper resolved those errors without changing tests or source.

The inventory conclusions are `documented-only`; the current suite is
`example-tested` within its fixtures. This run is not fresh PostgreSQL integration, HTTP
fault injection, formal verification, a new physical-crash campaign, cross-host
qualification or deployment. The 11 skips remain unexecuted evidence. Older
verification packets retain their original implementation hashes, classifications
and limits; this inventory does not rewrite them into current qualification.

Offline integrity can be checked against retained Git objects. From an
Auditctl checkout with this report present:

```python
import hashlib, json, subprocess
from pathlib import Path
packet = json.loads(Path("docs/evidence/ec07-interface-inventory-20261018.json").read_text())
for path, expected in packet["source_files_sha256"].items():
    raw = subprocess.check_output(["git", "show", packet["source_commit"] + ":" + path])
    assert hashlib.sha256(raw).hexdigest() == expected, path
print("captured Auditctl source bytes match")
```

The external edge entries carry their own repository, immutable commit, path
and byte digest; apply the same check in each named checkout. This proves
retained-byte integrity, not deployed provenance or complete consumer coverage.

Completion of this discovery means an inspectable inventory only. Auditctl#2740
remains pending for canonical generation, implementation/onboarding, version and
failure qualification, separately authorized cutover and rollback, and removal
of superseded interfaces. No service deployment is requested.
