# Central audit schema operations

Central migrations are an explicit deployment action. Neither `auditctl` local
startup nor the served runtime calls `migrate()` automatically.

Auditctl 0.1.5 uses the immutable `vuoro-schema-runtime` 0.1.0 wheel for
database-independent migration asset metadata, UTF-8 SHA-256 calculation,
contiguous-version validation, strict PostgreSQL identifier quoting, schema
placeholder rendering, and the pure migration-ledger verdict. Auditctl remains
the authority for its SQL assets, PostgreSQL connections and transactions,
advisory locks, ledger I/O and errors, compatibility response and reason
strings, DDL, grants, principal recording, and runtime-role rotation.

## Deployment command

Install the service-side distribution with the `central` extra, supply the DSN
through a secret-backed environment variable, and identify both principals:

```bash
export AUDITCTL_CENTRAL_MIGRATION_DSN='postgresql://...'
auditctl-central-schema migrate \
  --schema audit \
  --migration-role vuoro_audit_migration \
  --runtime-role vuoro_audit_runtime
```

The command refuses a connection whose `current_user` is not the declared
migration role. It takes a PostgreSQL transaction advisory lock scoped to the
audit schema, verifies the recorded SHA-256 for every previously applied SQL
asset, applies only missing versions, reasserts least-privilege grants, and
prints a JSON result containing the installed and newly applied versions.
Repeated execution at the current version is a successful no-op.
Re-running with a new runtime role atomically revokes the previous runtime
principal before recording and granting the replacement.

The database authority must create the login roles, database, DSN secret, and
schema-creation grant before this command. The migration never creates login
roles or stores credentials. One isolated schema or database is required per
environment; appservice can use independent DSNs without changing this code.

## Rollout gate

Run the migration job before starting or updating the service, then check the
runtime connection separately:

```bash
export AUDITCTL_CENTRAL_RUNTIME_DSN='postgresql://...'
auditctl-central-schema check-compatibility \
  --schema audit \
  --expected-role-kind runtime
```

The check is read-only, emits a machine-readable compatibility record, and
exits 2 when incompatible. A migration-role check can be used by the Job after
migration, but that identity still fails a runtime-role check. Deployment logs
and Job status provide migration observability; service rollout must be gated
on both the Job and runtime compatibility result.

## Upgrade, rollback, and recovery

Migrations are forward-only and data-preserving. Version 2 backfills stable
receipts for any version-1 observations. A checksum mismatch stops the Job;
operators must restore the released migration asset rather than rewrite the
ledger. A package older than the installed schema also refuses to migrate.

Application rollback is safe only while the earlier service declares the
installed schema compatible. Database rollback uses the deployment authority's
backup/restore procedure; the migration entrypoint does not run down-migrations.
If the central database is unavailable or restored, local NDJSON shards remain
the source for delayed upload, exact retry, and reconstruction. Recovery must
not synthesize missing observations or create authority transitions.
