# Offline source qualification, 2026-10-18

This implementation tranche follows the workspace target state TS-6 and the
2026-10-15 Vuoro product direction: preserve historical authored evidence and
qualify it for a verified import into the existing evidence authority. TS-13's
kctl lesson migration remains proposed. Neither tool gains a new lifecycle or
an independent hosted evidence home.

## Backlog evidence and coordination

Both repositories' GitHub open-issue queries returned empty lists. Their live
served sprint/backlog reads returned catalog HTTP 503 outside the sandbox.
Deployment session `01a14da7-de27-7071-a890-e6c5554bf0ee` confirmed that the shared
API is intentionally fenced during agentops #2654 backup/restore and cutover;
it also owns #2535 integration and all service deployments. Live sprint state
and reservations could not be checked. No replacement queue, reservation or
tracker write was created. Existing alignment plans name auditctl #1201/#1202
and kctl #1199/#1200; those historical references do not establish current status.

The deployment owner confirmed no overlapping auditctl/kctl source edits.
This tranche changes offline input qualification only. Package versions,
service dependency pins, migrations and deployment state stay with their
current owners.

## Optimistic planner review and delivered scope

A separate optimistic planner recommended attainable preservation work over
new product surfaces, then reviewed both implementation diffs with no blocking
findings:

- Auditctl refuses manifests declaring more than 100,000 source lines before
  opening source files. Duplicate copies count toward the bound. Iterated line
  framing preserves raw hashes and byte offsets while avoiding full line lists.
- Kctl's existing central-transfer reader and writer enforce a 64 MiB byte
  bound. Reads require regular files and strict UTF-8 JSON; ambiguous keys and
  non-finite numbers fail validation. Valid canonical bytes and digests retain
  their existing format. This is existing-schema recovery validation, not TS-13
  or S4 admission.

Regression tests demonstrate the former missing refusals, boundary acceptance,
canonical Unicode byte compatibility, and preservation of source framing.
Fixtures use temporary files and databases; no shared audit events are emitted.

## Follow-up when live state is available

Reconcile these findings with the served backlog after the deployment owner
restores the API. Source-only qualification is insufficient for an S4 cutover:
Git/protected-tail provenance, destination admission, complete-inventory
rehearsal and source-to-destination reconciliation remain with their owners.
Larger source sets need a reviewed qualification strategy; independent reports
for fragments of one producer stream do not prove complete stream coverage.

Separate sessions dispatched at the user's request explore refactoring needs
(`01a14db0-5c05-74d2-968b-e761c50a60a2`) and sprintctl innovation from session
and UX evidence (`01a14db0-8f4e-7e70-814a-2be9b86d6ca4`). Those explorations do
not authorize deployments or duplicate this tranche's source edits.
