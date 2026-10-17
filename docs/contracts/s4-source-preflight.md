# S4 source snapshot preflight

`auditctl source-preflight` is a read-only source qualification command for a
later S4 import review. It verifies only explicitly named files, uses
Auditctl's own event and central-observation validators, and emits a bounded
JSON report. It does not search for shards, use the configured SQLite database,
write records, contact a central store, import data, or authorize a cutover.

The caller supplies a manifest file and its separately recorded SHA-256:

```sh
auditctl source-preflight \
  --manifest /private/review/frozen-sources.json \
  --manifest-sha256 <sha256-of-exact-manifest-bytes>
```

The exact manifest object is:

```json
{
  "schema_version": "auditctl-source-snapshot-manifest/v1",
  "sources": [
    {
      "source_id": "repo-a-2026-10-01",
      "source_repo_id": "repo-a",
      "scope": "committed-shard",
      "path": "events-2026-10-01.ndjson",
      "byte_count": 12345,
      "line_count": 27,
      "sha256": "<lowercase SHA-256 of exact file bytes>"
    }
  ]
}
```

Relative paths resolve only against the manifest directory. Every path must be
an explicit regular file. The tool checks the externally supplied manifest
digest, each whole-file byte count and digest, final newline, and exact line
count before parsing. Reports include source ids, scope, counts and digests,
not local paths or event payloads. Files and manifests are bounded to 512 MiB
and 4 MiB respectively.
The `scope` value is a manifest assertion; this command does not verify Git
blob/commit provenance or whether a tail is protected. Use the Agentops overlay
verifier for those checks before treating the inventory as reviewed.

Each line is parsed as strict UTF-8 JSON with duplicate object keys and
non-finite numbers refused. `validate_event_object` checks the original
Auditctl event. A present envelope must be complete and valid; a legacy event
without the envelope remains legacy. The report records every line's source
id, line number, byte offset and raw-line SHA-256. It distinguishes an absent
source `payload_sha256` or `record_sha256` from a derived digest. For complete
envelopes, `prepare_observation` validates the payload digest and supplies a
separately named `auditctl-central-v1` record digest. A supplied source
`record_sha256` is compared only in that domain; for a legacy row its domain
is unknown and the report does not relabel or validate it. The import
fingerprint has its own explicit domain and never replaces an authored digest.

Repeated stable event ids are counted as exact duplicate copies only when the
raw line bytes and source repository identity match. Different bytes or repo
identity conflict. Reused producer `(origin_stream_id, origin_seq)` tuples,
producer sequences that do not form a contiguous range beginning at 1, and
reused explicit `idempotency_key` values bound to different records fail the
preflight. The collision check applies only to an
explicit source-level `idempotency_key`; Auditctl's producer idempotency key is
request context and is not embedded in ordinary shard events. The report sets
`qualification=source-only-qualified`, `s4_authority_established=false`, and
`cutover_authorized=false`; passing it is not full S4 qualification. Agentops
overlay validation remains the owner of Git blob and protected-tail provenance
verification, while Sprintctl remains the owner of destination admission,
authority storage, import receipts and cutover protocol.
