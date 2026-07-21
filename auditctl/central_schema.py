from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from importlib import resources
from typing import Any, Sequence

CURRENT_SCHEMA_VERSION = 2
MIN_RUNTIME_SCHEMA_VERSION = 2
MAX_RUNTIME_SCHEMA_VERSION = 2
DOMAIN_API_VERSION = "audit/v1"
MIGRATION_LOCK_NAMESPACE = "auditctl-central-schema"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class CentralSchemaError(RuntimeError):
    """Base error for explicit central-schema operations."""


class MigrationRoleError(CentralSchemaError):
    """The migration entrypoint is running as an unexpected database role."""


class MigrationDriftError(CentralSchemaError):
    """An already-applied migration no longer has its recorded checksum."""


class SchemaCompatibilityError(CentralSchemaError):
    """The served audit runtime cannot safely use the selected schema."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    sha256: str


@dataclass(frozen=True)
class MigrationResult:
    schema: str
    installed_version: int
    applied_versions: tuple[int, ...]
    migration_role: str
    runtime_role: str


@dataclass(frozen=True)
class Compatibility:
    schema: str
    domain_api_version: str
    installed_schema_version: int | None
    minimum_schema_version: int
    maximum_schema_version: int
    current_role: str
    expected_role_kind: str
    configured_role: str | None
    compatible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


def _identifier(value: str, field: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} must be an unquoted PostgreSQL identifier")
    return value


def _quoted(value: str, field: str) -> str:
    return f'"{_identifier(value, field)}"'


def load_migrations() -> tuple[Migration, ...]:
    root = resources.files("auditctl.central_migrations").joinpath("versions")
    migrations: list[Migration] = []
    for asset in sorted(root.iterdir(), key=lambda item: item.name):
        if not asset.name.endswith(".sql"):
            continue
        prefix, _, name = asset.name.partition("_")
        version = int(prefix)
        sql = asset.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=version,
                name=name.removesuffix(".sql"),
                sql=sql,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
        raise CentralSchemaError(
            f"migration assets must be contiguous through {CURRENT_SCHEMA_VERSION}: {versions}"
        )
    return tuple(migrations)


def _current_user(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        row = cur.fetchone()
    return str(row[0] if not isinstance(row, dict) else row["current_user"])


def _bootstrap_ledger(conn: Any, schema: str) -> None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_ident}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {schema_ident}.schema_migration (
                version     integer     PRIMARY KEY CHECK (version > 0),
                name        text        NOT NULL,
                sha256      text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
                applied_at  timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )


def _applied_migrations(conn: Any, schema: str) -> dict[int, tuple[str, str]]:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, name, sha256 FROM {schema_ident}.schema_migration ORDER BY version"
        )
        rows = cur.fetchall()
    values: dict[int, tuple[str, str]] = {}
    for row in rows:
        if isinstance(row, dict):
            values[int(row["version"])] = (str(row["name"]), str(row["sha256"]))
        else:
            values[int(row[0])] = (str(row[1]), str(row[2]))
    return values


def _record_principals(
    conn: Any, *, schema: str, migration_role: str, runtime_role: str
) -> None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema_ident}.schema_principal (role_kind, role_name)
            VALUES ('migration', %s), ('runtime', %s)
            ON CONFLICT (role_kind) DO UPDATE
            SET role_name = EXCLUDED.role_name, recorded_at = clock_timestamp()
            """,
            (migration_role, runtime_role),
        )


def _configured_principal(conn: Any, *, schema: str, role_kind: str) -> str | None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT role_name::text FROM {schema_ident}.schema_principal WHERE role_kind = %s",
            (role_kind,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0] if not isinstance(row, dict) else next(iter(row.values())))


def _revoke_rotated_runtime(
    conn: Any, *, schema: str, old_runtime_role: str, runtime_role: str
) -> None:
    if old_runtime_role == runtime_role:
        return
    schema_ident = _quoted(schema, "schema")
    old_runtime_ident = _quoted(old_runtime_role, "recorded runtime_role")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (old_runtime_role,))
        if cur.fetchone() is None:
            return
        cur.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema_ident} FROM {old_runtime_ident}"
        )
        cur.execute(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema_ident} FROM {old_runtime_ident}"
        )
        cur.execute(f"REVOKE ALL ON SCHEMA {schema_ident} FROM {old_runtime_ident}")


def _configure_runtime_privileges(
    conn: Any, *, schema: str, migration_role: str, runtime_role: str, installed: int
) -> None:
    schema_ident = _quoted(schema, "schema")
    runtime_ident = _quoted(runtime_role, "runtime_role")
    migration_ident = _quoted(migration_role, "migration_role")
    statements = [
        f"REVOKE ALL ON SCHEMA {schema_ident} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {schema_ident} TO {runtime_ident}",
        f"REVOKE CREATE ON SCHEMA {schema_ident} FROM {runtime_ident}",
        f"REVOKE ALL ON {schema_ident}.schema_migration FROM PUBLIC, {runtime_ident}",
        f"GRANT SELECT ON {schema_ident}.schema_migration TO {runtime_ident}",
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} IN SCHEMA {schema_ident} "
            "REVOKE ALL ON TABLES FROM PUBLIC"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} IN SCHEMA {schema_ident} "
            "REVOKE ALL ON SEQUENCES FROM PUBLIC"
        ),
    ]
    if installed >= 1:
        statements.extend(
            [
                f"REVOKE ALL ON {schema_ident}.ingest_stream FROM PUBLIC, {runtime_ident}",
                f"GRANT SELECT, INSERT, UPDATE ON {schema_ident}.ingest_stream TO {runtime_ident}",
                f"REVOKE ALL ON {schema_ident}.ingest_observation FROM PUBLIC, {runtime_ident}",
                f"GRANT SELECT, INSERT ON {schema_ident}.ingest_observation TO {runtime_ident}",
                (
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema_ident} "
                    f"TO {runtime_ident}"
                ),
            ]
        )
    if installed >= 2:
        statements.extend(
            [
                f"REVOKE ALL ON {schema_ident}.ingest_receipt FROM PUBLIC, {runtime_ident}",
                f"GRANT SELECT, INSERT, UPDATE ON {schema_ident}.ingest_receipt TO {runtime_ident}",
                f"REVOKE ALL ON {schema_ident}.schema_principal FROM PUBLIC, {runtime_ident}",
                f"GRANT SELECT ON {schema_ident}.schema_principal TO {runtime_ident}",
            ]
        )
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def migrate(
    conn: Any,
    *,
    schema: str,
    migration_role: str,
    runtime_role: str,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> MigrationResult:
    """Apply audit migrations explicitly under the deployment migration role.

    The advisory transaction lock serializes jobs per schema. This function is
    never called from auditctl startup or the local capture path.
    """
    _identifier(schema, "schema")
    _identifier(migration_role, "migration_role")
    _identifier(runtime_role, "runtime_role")
    if migration_role == runtime_role:
        raise ValueError("migration_role and runtime_role must be different roles")
    if target_version < 1 or target_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"target_version must be between 1 and {CURRENT_SCHEMA_VERSION}"
        )
    applied_now: list[int] = []
    with conn.transaction():
        current_user = _current_user(conn)
        if current_user != migration_role:
            raise MigrationRoleError(
                f"migration connection role is {current_user!r}, expected {migration_role!r}"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{MIGRATION_LOCK_NAMESPACE}:{schema}",),
            )
        _bootstrap_ledger(conn, schema)
        applied = _applied_migrations(conn, schema)
        applied_versions = sorted(applied)
        if applied_versions and applied_versions != list(
            range(1, applied_versions[-1] + 1)
        ):
            raise MigrationDriftError("migration ledger versions are not contiguous")
        if applied_versions and applied_versions[-1] > CURRENT_SCHEMA_VERSION:
            raise MigrationDriftError(
                f"installed schema version {applied_versions[-1]} is newer than this package"
            )
        migrations = load_migrations()
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, migration.sha256):
                    raise MigrationDriftError(
                        f"migration {migration.version} checksum or name does not match the ledger"
                    )
                continue
            if migration.version > target_version:
                break
            schema_ident = _quoted(schema, "schema")
            rendered = migration.sql.replace("__SCHEMA__", schema_ident)
            with conn.cursor() as cur:
                cur.execute(rendered)
                cur.execute(
                    f"""
                    INSERT INTO {schema_ident}.schema_migration (version, name, sha256)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.sha256),
                )
            applied_now.append(migration.version)

        installed = max(_applied_migrations(conn, schema), default=0)
        if installed >= 2:
            old_runtime_role = _configured_principal(
                conn, schema=schema, role_kind="runtime"
            )
            if old_runtime_role is not None:
                _revoke_rotated_runtime(
                    conn,
                    schema=schema,
                    old_runtime_role=old_runtime_role,
                    runtime_role=runtime_role,
                )
            _record_principals(
                conn,
                schema=schema,
                migration_role=migration_role,
                runtime_role=runtime_role,
            )
        _configure_runtime_privileges(
            conn,
            schema=schema,
            migration_role=migration_role,
            runtime_role=runtime_role,
            installed=installed,
        )
    return MigrationResult(
        schema=schema,
        installed_version=installed,
        applied_versions=tuple(applied_now),
        migration_role=migration_role,
        runtime_role=runtime_role,
    )


def check_compatibility(
    conn: Any, *, schema: str, expected_role_kind: str = "runtime"
) -> Compatibility:
    """Read schema and role compatibility without creating or migrating state."""
    _identifier(schema, "schema")
    if expected_role_kind not in {"runtime", "migration"}:
        raise ValueError("expected_role_kind must be runtime or migration")
    schema_ident = _quoted(schema, "schema")
    current_user = _current_user(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT has_schema_privilege(current_user, oid, 'USAGE') AS has_usage
            FROM pg_namespace
            WHERE nspname = %s
            """,
            (schema,),
        )
        namespace = cur.fetchone()
    if namespace is None:
        return Compatibility(
            schema=schema,
            domain_api_version=DOMAIN_API_VERSION,
            installed_schema_version=None,
            minimum_schema_version=MIN_RUNTIME_SCHEMA_VERSION,
            maximum_schema_version=MAX_RUNTIME_SCHEMA_VERSION,
            current_role=current_user,
            expected_role_kind=expected_role_kind,
            configured_role=None,
            compatible=False,
            reasons=("schema_not_initialized",),
        )
    has_usage = bool(
        namespace[0] if not isinstance(namespace, dict) else namespace["has_usage"]
    )
    if not has_usage:
        return Compatibility(
            schema=schema,
            domain_api_version=DOMAIN_API_VERSION,
            installed_schema_version=None,
            minimum_schema_version=MIN_RUNTIME_SCHEMA_VERSION,
            maximum_schema_version=MAX_RUNTIME_SCHEMA_VERSION,
            current_role=current_user,
            expected_role_kind=expected_role_kind,
            configured_role=None,
            compatible=False,
            reasons=("schema_access_denied",),
        )
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.schema_migration",))
        row = cur.fetchone()
        ledger_exists = (
            row[0] if not isinstance(row, dict) else next(iter(row.values()))
        ) is not None
    if not ledger_exists:
        return Compatibility(
            schema=schema,
            domain_api_version=DOMAIN_API_VERSION,
            installed_schema_version=None,
            minimum_schema_version=MIN_RUNTIME_SCHEMA_VERSION,
            maximum_schema_version=MAX_RUNTIME_SCHEMA_VERSION,
            current_role=current_user,
            expected_role_kind=expected_role_kind,
            configured_role=None,
            compatible=False,
            reasons=("schema_not_initialized",),
        )

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT max(version) AS version FROM {schema_ident}.schema_migration"
        )
        row = cur.fetchone()
    raw_version = row[0] if not isinstance(row, dict) else row["version"]
    installed = int(raw_version or 0)
    reasons: list[str] = []
    if installed < MIN_RUNTIME_SCHEMA_VERSION:
        reasons.append("schema_too_old")
    if installed > MAX_RUNTIME_SCHEMA_VERSION:
        reasons.append("schema_too_new")

    configured_role: str | None = None
    version_compatible = (
        MIN_RUNTIME_SCHEMA_VERSION <= installed <= MAX_RUNTIME_SCHEMA_VERSION
    )
    if version_compatible:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT role_name::text FROM {schema_ident}.schema_principal WHERE role_kind = %s",
                (expected_role_kind,),
            )
            row = cur.fetchone()
        if row is not None:
            configured_role = str(
                row[0] if not isinstance(row, dict) else next(iter(row.values()))
            )
        if configured_role is None:
            reasons.append("role_not_configured")
        elif current_user != configured_role:
            reasons.append("role_kind_mismatch")

    return Compatibility(
        schema=schema,
        domain_api_version=DOMAIN_API_VERSION,
        installed_schema_version=installed,
        minimum_schema_version=MIN_RUNTIME_SCHEMA_VERSION,
        maximum_schema_version=MAX_RUNTIME_SCHEMA_VERSION,
        current_role=current_user,
        expected_role_kind=expected_role_kind,
        configured_role=configured_role,
        compatible=not reasons,
        reasons=tuple(reasons),
    )


def require_runtime_compatibility(conn: Any, *, schema: str) -> Compatibility:
    result = check_compatibility(conn, schema=schema, expected_role_kind="runtime")
    if not result.compatible:
        reasons = ", ".join(result.reasons)
        raise SchemaCompatibilityError(
            f"central audit schema is incompatible: {reasons}"
        )
    return result


def _connect(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise CentralSchemaError(
            "central schema commands require auditctl[central]"
        ) from exc
    return psycopg.connect(dsn, row_factory=dict_row)


def _env_dsn(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CentralSchemaError(f"{name} is required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auditctl-central-schema")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate")
    migrate_parser.add_argument("--schema", default="audit")
    migrate_parser.add_argument("--migration-role", required=True)
    migrate_parser.add_argument("--runtime-role", required=True)
    migrate_parser.add_argument(
        "--target-version", type=int, default=CURRENT_SCHEMA_VERSION
    )
    migrate_parser.add_argument("--dsn-env", default="AUDITCTL_CENTRAL_MIGRATION_DSN")
    check_parser = commands.add_parser("check-compatibility")
    check_parser.add_argument("--schema", default="audit")
    check_parser.add_argument(
        "--expected-role-kind", choices=("runtime", "migration"), default="runtime"
    )
    check_parser.add_argument("--dsn-env", default="AUDITCTL_CENTRAL_RUNTIME_DSN")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = _env_dsn(args.dsn_env)
    with _connect(dsn) as conn:
        if args.command == "migrate":
            result = migrate(
                conn,
                schema=args.schema,
                migration_role=args.migration_role,
                runtime_role=args.runtime_role,
                target_version=args.target_version,
            )
            print(json.dumps(asdict(result), sort_keys=True))
            return 0
        result = check_compatibility(
            conn, schema=args.schema, expected_role_kind=args.expected_role_kind
        )
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 0 if result.compatible else 2
    raise AssertionError("unreachable")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
