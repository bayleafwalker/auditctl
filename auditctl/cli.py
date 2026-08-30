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
from .paths import resolve_audit_context, resolve_paths, shard_path
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
        # One resolution, consumed. Do not reintroduce a separately-resolved root here:
        # that split is what misrouted 13 events on 2026-08-29.
        context = resolve_audit_context()
        published_from = Path.cwd()
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
                # The resolver's account of this write, not the publisher's. A caller
                # cannot supply or suppress it; that is the point of recording it here
                # rather than leaving it to publishers and their metadata dictionaries.
                "resolved_context": context.as_record(published_from),
            }
        )
        ndjson_path = context.shard_for(timestamp)
        conn = db.connect(context.index_path)
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
                    "repo_id": context.repo_id,
                    "ndjson_path": str(ndjson_path),
                    "resolution_source": context.resolution_source,
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


def _index_only_message(index_only, *, shard_count: int, shard_events: int) -> str:
    """Say exactly what is missing, so the operator can find the publisher."""

    sources = sorted({str(event.get("source") or "unknown") for event in index_only})
    dates = sorted({str(event.get("ts") or "")[:10] for event in index_only})
    sample = ", ".join(str(event["id"]) for event in index_only[:3])
    return (
        f"rebuild rejected [index_only_events]: the index holds {len(index_only)} "
        f"event(s) that no shard carries, so rebuilding from "
        f"{shard_count} shard(s) / {shard_events} event(s) would drop them.\n"
        f"  sources: {', '.join(sources)}\n"
        f"  dates:   {', '.join(dates)}\n"
        f"  first:   {sample}\n"
        "Shards are authoritative: find the publisher that indexed without "
        "appending and re-emit, or pass --allow-index-only to accept the loss."
    )


@cli.command("rebuild")
@click.option("--from-ndjson", "from_ndjson", required=True, help="Shard file, directory, or glob")
@click.option("--replace", is_flag=True, default=False, help="Replace current sqlite db after creating a backup")
@click.option("--dry-run", is_flag=True, default=False, help="Validate only")
@click.option(
    "--allow-index-only",
    is_flag=True,
    default=False,
    help="Proceed even though the index holds events no shard carries (they are lost)",
)
def rebuild_cmd(from_ndjson, replace, dry_run, allow_index_only) -> None:
    """Rebuild sqlite from NDJSON shards."""
    try:
        paths = resolve_paths()
        input_paths = resolve_inputs(from_ndjson)
        if not input_paths:
            raise ValueError("no NDJSON shards matched")
        events = list(read_events(input_paths))
        # Coverage is a separate question from validity.  The batch validation
        # only inspects ids the batch names, so a shard that was never written
        # -- or a publisher that indexed without appending -- passes it while
        # the rebuild silently drops those events.  It is computed first because
        # those same events left holes in the sequence, and continuity may only
        # skip a hole the caller has explicitly accepted.
        index_only = db.index_only_events(paths.db_path, events)
        accepted_missing_seqs = frozenset(
            event["origin_seq"]
            for event in index_only
            if allow_index_only and event.get("origin_seq") is not None
        )
        # This is intentionally before --replace moves the current DB.  A rejected
        # batch is read-only with respect to its source, destination, and cursor.
        db.validate_import_batch(
            paths.db_path,
            events,
            against_existing=not replace,
            accepted_missing_seqs=accepted_missing_seqs,
        )
    except (ImportInputError, db.ImportValidationError) as exc:
        raise click.ClickException(f"rebuild rejected [{exc.code}]") from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if index_only and not allow_index_only:
        raise click.ClickException(
            _index_only_message(index_only, shard_count=len(input_paths), shard_events=len(events))
        )

    if dry_run:
        summary = f"Validated {len(input_paths)} shard(s): {len(events)} event(s)."
        if index_only:
            summary += f" WARNING: {len(index_only)} index-only event(s) will be lost."
        click.echo(summary)
        return

    if replace and paths.db_path.exists():
        backup = paths.db_path.with_name(paths.db_path.name + f".bak-{_now().replace(':', '').replace('-', '')}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(paths.db_path), str(backup))

    conn = db.connect(paths.db_path)
    try:
        db.init_db(conn)
        imported, skipped = db.import_events(conn, events, accepted_missing_seqs)
    finally:
        conn.close()
    message = f"Rebuilt audit db from {len(input_paths)} shard(s): {imported} imported, {skipped} skipped."
    if index_only:
        message += f" {len(index_only)} index-only event(s) were discarded on request."
    click.echo(message)


if __name__ == "__main__":
    cli()
