# auditctl

Repo-local audit ledger for the agent-ops substrate.

`auditctl` records human, agent, git, sprintctl, and actionq events into a local sqlite index and a durable daily NDJSON shard:

```text
<AUDITCTL_ARTIFACTS_ROOT>/_artifacts/<repo_id>/audit/events-YYYY-MM-DD.ndjson
```

The sqlite database is the fast local query index. The NDJSON shards are the portable recovery and cockpit-read artifact.

New events also carry the shared producer-observation envelope: a durable
`origin_stream_id`, monotonically allocated `origin_seq`, the existing audit
ID as `event_id`, mapped event/timestamp fields, and a canonical payload
digest. Existing shards without these additive fields remain readable and
rebuildable.

## Install

```bash
uv tool install /projects/dev/auditctl --python python3
```

## Configure a Repo

```bash
export AUDITCTL_DB="$PWD/.auditctl/auditctl.db"
export AUDITCTL_ARTIFACTS_ROOT="/projects/dev"
```

## Commands

```bash
auditctl add --type decision --actor bayleaf --summary "Chose sqlite plus NDJSON"
auditctl list --limit 10
auditctl render --format ndjson
auditctl rebuild --from-ndjson /projects/dev/_artifacts/homelab-analytics/audit
```

`auditctl add` requires `AUDITCTL_ARTIFACTS_ROOT`. Read commands do not.

## Git Hooks

Example hook scripts live in `hooks/`. Repos may copy or symlink them. V1 does not manage hook installation globally.
