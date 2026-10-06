# Optional verified-source attribution on ordinary auditctl list

The default `auditctl list` command retains its existing SQLite-backed JSON array
and text output. An explicit snapshot mode reports derived repository attribution
without opening, creating, migrating or rebuilding the index:

```sh
auditctl list --json \
  --attribution-overlay committed-map.json \
  --attribution-source source-id=frozen-events.ndjson \
  --attributed-repo vuoro-cloud
```

Repeat `--attribution-overlay` and `--attribution-source` to supply an explicit
protected-tail overlay with its frozen snapshot, or additional verified physical
copies. Overlay and source options are required together. This mode requires
`--json`. No alias/path/store discovery or automatic tail inclusion occurs.

The optional external dependency is an ordinary trusted installed `agentops`
command with the `query-audit-attribution` topic and `audit-attribution-query/v1`
interface. The source interface is pinned for verification to Agentops commit
`7b5c02a14bee88e7c19ed81f4f510800c0160a8c` (PR #309). This is a standalone tool,
not a Python package dependency. An auditctl wheel installs normally with its
existing kit/schema dependencies; snapshot mode fails visibly when the installed
tool/topic/interface is unavailable. It never downloads code, imports another
checkout, copies the canonical source validator or invokes a shell.

Snapshot JSON uses `auditctl-list-attribution/v1` and explicitly labels
`mode=verified-source-snapshots`. `verification_receipt` retains the helper's
coverage, overlay digests, whole-snapshot counts and append-extension boolean
flags. Those flags mean extra bytes exist and were not parsed; they are not byte
counts. `matched_events` and `returned_events` describe the filtered page.
Rows wrap unchanged `original_event` data with separate attribution fields.
Historical unknown fields, actor, source, class, payload, digests and metadata
are preserved. `--source` still filters the original event's source string;
`--attributed-repo` selects explicitly mapped events only. Type/time/repo filters
precede descending timestamp/id ordering and the existing default limit 50.
Negative limits are refused in snapshot mode. No derived repository value
changes producer identity, authorization, Decisions or effective work status.

The consumer pipes the local helper's stdout into a private temporary disk
spool before accepting a complete result. Its conservative finite response budget
is derived from actual supplied source-file byte lengths, declared overlay line
counts/repository token sizes, overlay bytes and argument lengths. This is a
serialization/resource bound, not a record-size admission rule or a claim that
all historical estate records were measured. Source validation remains owned by
the generic helper. Oversized or malformed/unknown/inconsistent responses fail
the entire report; output is never truncated or partially emitted. Only regular
files (including ordinary symlinks to them) are eligible; FIFOs,
devices and directories are refused before helper invocation. The subprocess has
a 300-second reporting deadline, configurable through
`AUDITCTL_ATTRIBUTION_TIMEOUT_SECONDS` to a positive value up to 3600 seconds.
A stalled helper is killed and reaped without partial output. This operational
deadline is a resource refusal, not an assertion about source integrity or estate
coverage. Source/index files are never changed by this path. Helper error payloads
and source contents are not logged on failure. This snapshot mode uses POSIX
file and process primitives, matching auditctl's existing filesystem platform.
A path replaced after eligibility checking still faces the generic source guards
and consumer deadline/budget; no replacement establishes verified attribution.

This is a source-backed ordinary CLI consumer. It does not join the SQLite index,
attach attribution to its default listing or read a central PostgreSQL store.
The exact raw source snapshots remain required for verification; parsed display
objects must not be reserialized as S4 import inputs. Agentops#2592 remains open
until its ordinary DB-backed consumer and actual S4 import requirements are
proven. Full S4 scope, retention, authenticated import allowance, digest-domain
preservation, causal absorb gate, hook cutover and soak are separate requirements.
