# Semantic evidence consumers: source preservation and asynchronous interpretation

Status: **operator-requested owner delivery plan**, 2026-10-25. No schema
migration, deployed scribe, collector change or cutover is established here.
Cross-estate AE-01/AE-02:
[Agentops evidence-guided work plan](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-10-25-evidence-guided-agent-workflows.md).
This narrows [AU-K01](2026-10-18-kaos-roadmap.md) rather than opening a
competing evidence-home project.

## Problem and current owner

Code sessions leave mechanical evidence (Git commits, test results, lifecycle
events, cost receipts), and optional later models may infer findings or lessons.
Auditctl currently owns local SQLite/NDJSON audit capture, stable producer
identity, the optional central observation/decision record envelope, and
the owner-provided adapter. It does **not** accept work, change Release status,
grant privileges, publish Workspace canon, or apply effects.

The existing [publisher subprocess contract](
../contracts/publisher-subprocess.md) is the integration boundary. Capture must
follow the authoritative source operation, through the binary/declared owner
adapter; no agent edits the SQLite, NDJSON or served PostgreSQL stores directly.
A non-atomic local SQLite+NDJSON interruption recovers from the original shards.
The [central observation-ingest contract](
../contracts/central-observation-ingest.md) has closed record classes
`observation` and `decision`. The latter is an immutable *judgment record*,
never an executable current work Decision. The S4 proposal converges future
evidence under Sprintctl's owner, retaining original historical bytes and
digests. S4 remains a separate measured import/cutover gate.

**Outctl and Kctl are excluded.** Neither a synchronous output proxy nor a new
knowledge-review lifecycle is needed. The semantic interpretation belongs to a
replaceable out-of-process evidence consumer. Auditctl should only change
where the source envelope or its provenance is genuinely insufficient.

## Implementation packets (audit owner)

### AU-SE1 — Inventory eligible facts and coverage [AE-01]

Read current native harness observability, provider/callback limitations,
Sprintctl publishing and Auditctl ingestion. Freeze a source-per-event matrix:
producer, stable origin identity, collection method, complete source reference,
content digest, privacy class, source timestamp, supported retries and
availability/absence states. Distinguish direct mechanical capture from a
session's self-report. Count the declared relevant sessions by host/harness,
not only events observed by the recorder.

**Acceptance:** one source-pinned local coding session, one synthetic
hosted-hook-missing session, CI/Git receipts and a recorder-failure fixture
yield a reproducible coverage report with correct denominators. The raw
prompt/transcript is opt-in, private and never emitted in telemetry.

### AU-SE2 — Contract-compatible semantic observation payload [AE-02]

Before changing an owner schema, try an ordinary versioned payload projected
through the current record contract. Proposed bounded fields:

```yaml
semantic_observation:
  schema_version: semantic-observation/v1  # candidate; not deployed API
  source_basis_ref: immutable-manifest-reference
  source_basis_sha256: sha256:...
  interpretation_key: sha256:...
  work_ref: wi:...
  observed_work_revision: 42
  statement: compatibility-risk-detected
  kind: model-inferred             # or deterministic-derived
  producer_model: provider/model@revision
  prompt_or_recipe_digest: sha256:...
  extracted_at: timestamp
  supersedes: null                 # optional previous inference ref
  applicability: exact-basis-and-work-revision
```

Freeze inputs in a resolvable source-basis manifest, not a single source
pointer. The manifest lists every source owner and immutable reference, the
digest of the original stored bytes and its digest domain, capture time,
privacy/retention class, and the applicable work revision. Hash the exact
frozen manifest bytes for `source_basis_sha256`; a digest without a permitted
reference to the source is insufficient. Keep original artifacts with their
source owners. Auditctl stores the bounded finding and basis reference; it
does not become an artifact store or recanonicalize historical bytes to make a
new digest.

Transport-specific shapes must be reconciled with EC-00 TypeSpec/OpenAPI and
the S4 work-evidence owner. This proposal does not introduce a third
`record_class` or create a parallel table. Source observations remain
immutable; invalidation/supersession is a *new recorded observation* or
rebuildable current view, never an overwrite. Keep `unknown`, absent and
unavailable distinct. A reader can fetch/check original source bytes where
permitted; agreement between two self-reports does not make verification
independent.

**Acceptance:** synthetic round-trip through current Auditctl serialization
and rebuild, hash/identity invariants, conflict/malformed input refusals;
legacy shard replay unchanged. Before S4, experiment output stays in
owner-private test artifacts, not a new canonical hosted audit database.

### AU-SE3 — Optional async interpretation and reuse [AE-02]

Run an out-of-process consumer over a frozen, authorized source set. Compare
deterministic extraction, one cheap general model (Qwen or DeepSeek) and,
if available, a typed extractor such as Clairic. Compare to simply retrieving
the original data for each consumer; do not make a model request from a
tool/session hook. Use the **same immutable source digest and two real
independent consumers**: work-completion assessment and retrospective
research/operational analysis. Record chosen provider, exact model revision,
profile, usage/billing provenance, retry and case identity. No implicit
provider failover or unauthorized outbound content.

The `interpretation_key` identifies one analysis intent: case ID, source-basis
reference and digest, work reference and revision, extraction method, recipe
digest, requested model/profile, and an explicit analysis generation. Exact
retries reuse that key; changing the basis or analysis configuration, or
deliberately requesting a fresh analysis, creates a new key and a new immutable
observation linked by `supersedes`. The key is a semantic deduplication aid;
Auditctl still mints its own event and producer identities.

Run enrichment only after the source owner commits. It cannot delay or change
that source operation. The consumer's existing runner/outbox owns durable
per-key claims and attempt status; failed, timed-out, unavailable, or denied
attempts remain operational status, not negative semantic observations. If no
existing owner mechanism can safely schedule and reconcile the work, keep this
packet at offline replay until one is identified; do not add a queue or job
database to Auditctl or Agentops. An unavailable source/model result stays
`unknown` or `unavailable`, never `no finding`.

The current publisher contract makes each `auditctl add` invocation a new
event, so the consumer must reconcile `interpretation_key` before retrying an
ambiguous local publish and must serialize simultaneous attempts for the same
key through its owner-side claim. For a lost central-ingest response, retry the
same original local envelope from NDJSON so the central receipt contract can
return the existing receipt; do not mint a replacement event. A changed basis
leaves the old finding inspectable, while a consumer must reject it for any
assessment whose current source-basis digest or work revision differs.

**Acceptance:** both consumers can resolve and challenge one finding against
all source bytes in its frozen basis manifest. Replay the same key and race
two deliveries for it: only one semantic observation results. Inject a lost
local publisher response and a lost central response; reconciliation must
avoid a second local observation and reuse the original central receipt.
Changing the source manifest or analysis generation yields a distinct linked
observation, and consumers ignore stale basis/revision matches. Missing,
denied, or unavailable models never prevent already-authorized work
completion. Stop if consumers rarely reuse the projection or it loses
critical evidence/costs more than repeated reads.

### AU-SE4 — Qualification, S4 handover and rollback

- Inject missing Auditctl binary, nonzero subprocess, timeout, duplicate
  caller retry, same-key concurrent delivery, ambiguous local publish response,
  NDJSON-ahead recovery, lost HTTP response, origin sequence gap, provider
  outage, denied source, source revision change and independent finding
  disagreement.
- Preserve original producer `event_id`, source `payload_sha256`, existing
  central record digest and commit/shard provenance. Do not recalculate a
  historical digest under the new envelope.
- Continue operating locally when central ingestion is unreachable; a
  successful work owner write stays successful after a recorder failure.
- Only the owning S4 migration plan may authorize cutover/permission changes.
  This plan provides qualified projection fixtures and an import mapping,
  not a second append authority.
- Rollback disables consumer jobs, leaves all original records intact,
  invalidates derived projections and restores source-based reads.

**Delivery handoff:** reconcile packets against served Sprintctl items and
EC-01 inventory. Existing item `agentops#2485` remains the S4 import track;
AU-SE1/2/3 are separate implementation requirements, not fabricated new
item numbers. Mark which source/release/deployment/field results are evidenced,
not inferred from documentation.
