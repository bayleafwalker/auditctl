from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import db
from .ids import new_event_id
from .ndjson import ImportInputError, append_event, read_events, resolve_inputs
from .paths import require_artifacts_root, resolve_paths, shard_path
from .render import render_text
from .validation import (
    canonical_json,
    parse_metadata,
    validate_event_object,
    validate_refs,
    validate_timestamp,
    with_observation_envelope,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_db():
    paths = resolve_paths()
    conn = db.connect(paths.db_path)
    db.init_db(conn)
    return paths, conn


def _reconcile_shards_before_allocation(conn, shard_dir: Path) -> None:
    """Recover an NDJSON-ahead crash while holding the SQLite writer lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        input_paths = resolve_inputs(str(shard_dir)) if shard_dir.exists() else []
        for event in read_events(input_paths):
            db.insert_event_ignore(conn, event)
            db.observe_origin(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@click.group()
def cli() -> None:
    """Repo-local audit ledger."""


@cli.command("add")
@click.option("--type", "type_", required=True, help="Audit event type")
@click.option("--actor", required=True, help="Actor responsible for the event")
@click.option("--summary", required=True, help="One-line event summary")
@click.option("--detail", default=None, help="Optional Markdown detail")
@click.option("--ref", "refs", multiple=True, help="Reference such as sha:<hash> or wi:<id>")
@click.option("--source", default="manual", show_default=True, help="Publisher source")
@click.option("--metadata", default=None, help="JSON object with publisher metadata")
@click.option("--ts", default=None, help="ISO UTC timestamp ending in Z")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit JSON")
def add_cmd(type_, actor, summary, detail, refs, source, metadata, ts, output_json) -> None:
    """Add one audit event to sqlite and today's NDJSON shard."""
    try:
        artifacts_root = require_artifacts_root()
        timestamp = validate_timestamp(ts or _now())
        created_at = _now()
        event = validate_event_object(
            {
                "id": new_event_id(),
                "ts": timestamp,
                "type": type_,
                "actor": actor,
                "summary": summary,
                "detail": detail,
                "refs": validate_refs(list(refs)),
                "source": source,
                "metadata": parse_metadata(metadata),
                "created_at": created_at,
            }
        )
        paths = resolve_paths()
        ndjson_path = shard_path(artifacts_root, paths.repo_id, timestamp)
        conn = db.connect(paths.db_path)
        db.init_db(conn)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        _reconcile_shards_before_allocation(conn, ndjson_path.parent)
        conn.execute("BEGIN IMMEDIATE")
        origin_stream_id, origin_seq = db.allocate_origin(conn)
        event = validate_event_object(
            with_observation_envelope(
                event,
                origin_stream_id=origin_stream_id,
                origin_seq=origin_seq,
            )
        )
        db.insert_event(conn, event)
        try:
            append_event(ndjson_path, event)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                click.echo(
                    f"auditctl: sqlite rollback failed after NDJSON append failure; audit_event {event['id']} may be inconsistent",
                    err=True,
                )
            raise click.ClickException(str(exc)) from exc
        try:
            conn.commit()
        except Exception as exc:
            click.echo(
                "auditctl: sqlite commit failed after NDJSON append; run auditctl rebuild --from-ndjson",
                err=True,
            )
            raise click.ClickException(str(exc)) from exc
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()

    if output_json:
        click.echo(
            json.dumps(
                {
                    "id": event["id"],
                    "event_id": event["event_id"],
                    "origin_stream_id": event["origin_stream_id"],
                    "origin_seq": event["origin_seq"],
                    "ts": event["ts"],
                    "type": event["type"],
                    "source": event["source"],
                    "repo_id": paths.repo_id,
                    "ndjson_path": str(ndjson_path),
                },
                sort_keys=True,
            )
        )
    else:
        click.echo(f"Added {event['id']} {event['type']} {event['source']} {event['ts']}")


@cli.command("list")
@click.option("--type", "type_", default=None, help="Filter by event type")
@click.option("--source", default=None, help="Filter by source")
@click.option("--since", default=None, help="Inclusive ISO UTC lower bound")
@click.option("--until", default=None, help="Inclusive ISO UTC upper bound")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "output_json", is_flag=True, default=False)
def list_cmd(type_, source, since, until, limit, output_json) -> None:
    """List audit events newest first."""
    try:
        if since:
            validate_timestamp(since)
        if until:
            validate_timestamp(until)
        _, conn = _open_db()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        events = db.query_events(conn, type_=type_, source=source, since=since, until=until, limit=limit)
    finally:
        conn.close()
    if output_json:
        click.echo(json.dumps(events, sort_keys=True))
        return
    if not events:
        click.echo("No audit events found.")
        return
    click.echo(f"{'TS':20}  {'ID':29}  {'TYPE':14}  {'SOURCE':12}  {'ACTOR':12}  SUMMARY")
    for event in events:
        click.echo(
            f"{event['ts']:20}  {event['id']:29}  {event['type'][:14]:14}  "
            f"{event['source'][:12]:12}  {event['actor'][:12]:12}  {event['summary']}"
        )


@cli.command("render")
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--type", "type_", default=None)
@click.option("--source", default=None)
@click.option("--format", "format_", type=click.Choice(["text", "ndjson"]), default="text", show_default=True)
@click.option("--limit", type=int, default=None)
def render_cmd(since, until, type_, source, format_, limit) -> None:
    """Render audit events chronologically."""
    try:
        if since:
            validate_timestamp(since)
        if until:
            validate_timestamp(until)
        _, conn = _open_db()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        events = db.query_events(
            conn,
            type_=type_,
            source=source,
            since=since,
            until=until,
            limit=limit,
            ascending=True,
        )
    finally:
        conn.close()
    if format_ == "ndjson":
        for event in events:
            click.echo(canonical_json(event))
    else:
        rendered = render_text(events)
        if rendered:
            click.echo(rendered)


@cli.command("rebuild")
@click.option("--from-ndjson", "from_ndjson", required=True, help="Shard file, directory, or glob")
@click.option("--replace", is_flag=True, default=False, help="Replace current sqlite db after creating a backup")
@click.option("--dry-run", is_flag=True, default=False, help="Validate only")
def rebuild_cmd(from_ndjson, replace, dry_run) -> None:
    """Rebuild sqlite from NDJSON shards."""
    try:
        paths = resolve_paths()
        input_paths = resolve_inputs(from_ndjson)
        if not input_paths:
            raise ValueError("no NDJSON shards matched")
        events = list(read_events(input_paths))
        # This is intentionally before --replace moves the current DB.  A rejected
        # batch is read-only with respect to its source, destination, and cursor.
        db.validate_import_batch(paths.db_path, events, against_existing=not replace)
    except (ImportInputError, db.ImportValidationError) as exc:
        raise click.ClickException(f"rebuild rejected [{exc.code}]") from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if dry_run:
        click.echo(f"Validated {len(input_paths)} shard(s): {len(events)} event(s).")
        return

    if replace and paths.db_path.exists():
        backup = paths.db_path.with_name(paths.db_path.name + f".bak-{_now().replace(':', '').replace('-', '')}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(paths.db_path), str(backup))

    conn = db.connect(paths.db_path)
    try:
        db.init_db(conn)
        imported, skipped = db.import_events(conn, events)
    finally:
        conn.close()
    click.echo(f"Rebuilt audit db from {len(input_paths)} shard(s): {imported} imported, {skipped} skipped.")


if __name__ == "__main__":
    cli()
