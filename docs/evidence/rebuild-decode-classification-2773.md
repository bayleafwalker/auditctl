# Typed shard decoding failures

Standalone Auditctl#2773 repairs an existing rejection classification at source
base `748dae7e6f3abdb361a6b18b5f4fbe69d4a4a2b5`. Native decision note5557 records
the explicit No doc scope; Release
`b9ba24b691458ecfd48df148a8f9d0166441126d2b8d0b49fe52d3caea3446f8`
authorize the bounded repair. The existing
[audit write/rebuild protocol draft](../protocols/audit-write-and-rebuild.md)
already requires typed corrupt-shard refusal before destination replacement.
Its draft status and historical verification packets remain unchanged.

Before repair, an invalid UTF-8 shard raised `UnicodeDecodeError` from the text
iterator. The CLI caught its generic ValueError base and printed the raw codec
message. A JSON decoder recursion failure escaped as `RecursionError`; the
in-process CLI returned no diagnostic, and an actual child process printed a
traceback. Neither case created the tested absent destination or changed its
input. This is a classification repair, not a newly established mutation or
cross-store atomicity boundary.

`read_events` now maps UTF-8 decoder failure and `json.loads` recursion failure
to the existing `ImportInputError("corrupt_shard")`. The existing CLI handler
therefore reports `Error: rebuild rejected [corrupt_shard]`. No input byte/depth
cap, historical format restriction, coercion or partial-line skipping was
added. Validation, stable event identity, cursor advancement, replacement,
capture/write ordering and central ingestion remain unchanged.

Before source edits, the new contract selection produced **30 failures and
2 passes in0.72s**. All failures concern absent typed reader/CLI classification;
the two controls already accepted valid large Unicode legacy/envelope records.
The final focused selection of decoding, rebuild and provenance tests produced
**88 passes in 1.46s**. The complete suite passed **267 tests with 11 skips in
7.81s**, using Python3.13.16, SQLite3.53.1 and pytest9.1.1. Existing optional
integration/platform skips remain unqualified; no central PostgreSQL run is
claimed by this local decoding change.

The compact observed baseline and RED-log digest are retained in
[`rebuild-decode-counterexample-2773.json`](../../verification/fixtures/rebuild-decode-counterexample-2773.json).
The full command used the externally reviewed Agentops attribution helper at
source7b5c02a, whose exact byte digest is recorded in the result packet:

```sh
AUDITCTL_TEST_ATTRIBUTION_QUERY=/tmp/auditctl-ec07-reviewed-attribution-query.py \
UV_PROJECT_ENVIRONMENT=/tmp/auditctl-corrupt-env UV_CACHE_DIR=/tmp/auditctl-corrupt-uv \
uv run --frozen --no-sync pytest -q --tb=short
```

The 32-case decoding selection comprises:

- Four direct-reader cases: invalid byte prefix, invalid tail after a valid
  record, truncated UTF-8 in a second shard, and decoder-recursive JSON.
- 24 CLI cases cross those four inputs with ordinary/dry-run/replace and
  absent/preexisting destinations. Exact source/destination bytes and file sets
  are compared; seeded rows and producer cursor are also checked through an
  independent immutable connection. No backup or sidecar is introduced by the
  rejected command.
- Two actual child-process replacement failures verify stable stderr instead
  of raw codec output/traceback, while all fixture files remain unchanged.
- Two valid historical large-Unicode cases retain exact content and stable-ID
  duplicate skipping across repeated rebuild, with and without an envelope.

The nesting fixture is20,000 levels; a smaller2,000-level exploratory array
parsed on the initial interpreter and correctly received the existing
`malformed_record` classification. That smaller input is not evidence of a
decoder failure or a new nesting rule. Decoder limits remain interpreter-owned.

The separate Depth2 packet covers real-filesystem input faults and two public
CLI child-process invocations. It establishes bounded example-tested failure
effects, not concurrent-writer, file-lock, fsync, process-kill, power-loss or
distributed guarantees. The old convergence packet is not refreshed or promoted.

All fixtures are temporary repositories and artifact roots. Genuine shared
authority outbox/terminal artifacts are preserved. No API/IDL/schema, service
deployment or EC-07 status/gate is changed. The existing protocol's broader
shard/line-reference wording is outside this stable-code repair; no new source
path or line diagnostic is claimed.
