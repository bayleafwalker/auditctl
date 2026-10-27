from __future__ import annotations

import json
import hashlib
import os
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
    canonical_payload_json,
    observation_payload,
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


@cli.command("source-preflight")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Explicit frozen source manifest",
)
@click.option(
    "--manifest-sha256",
    required=True,
    help="Externally recorded lowercase SHA-256 of the exact manifest bytes",
)
def source_preflight_cmd(manifest_path: Path, manifest_sha256: str) -> None:
    """Qualify an explicitly frozen source snapshot without opening the ledger database."""
    from .source_preflight import preflight

    try:
        report = preflight(manifest_path, manifest_sha256)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _record_event(
    event: dict,
    *,
    interpretation_key: str | None = None,
    immutable_input_paths: tuple[Path, ...] = (),
) -> tuple[dict, object, Path, bool]:
    """Append with the existing local writer, optionally deduping semantic replay."""
    context = resolve_audit_context()
    event = {
        **event,
        "id": new_event_id(),
        "created_at": _now(),
        "resolved_context": context.as_record(Path.cwd()),
    }
    event = validate_event_object(event)
    ndjson_path = context.shard_for(event["ts"])

    for input_path in immutable_input_paths:
        try:
            aliases_output = os.path.samefile(ndjson_path, input_path)
        except OSError:
            aliases_output = False
        if aliases_output or os.path.normcase(os.path.realpath(ndjson_path)) == os.path.normcase(
            os.path.realpath(input_path)
        ):
            raise ValueError("replay destination shard aliases an immutable replay input")

    conn = db.connect(context.index_path)
    db.init_db(conn)
    try:
        _reconcile_shards_before_allocation(conn, ndjson_path.parent)
        conn.execute("BEGIN IMMEDIATE")
        if interpretation_key is not None:
            semantic = event["metadata"]["semantic_observation"]
            supersedes = semantic["supersedes"]
            if supersedes is not None:
                prior = db.find_semantic_observation_by_id(conn, supersedes)
                prior_metadata = prior.get("metadata") if prior is not None else None
                prior_semantic = (
                    prior_metadata.get("semantic_observation")
                    if isinstance(prior_metadata, dict)
                    else None
                )
                if (
                    not isinstance(prior_semantic, dict)
                    or prior_semantic.get("case_id") != semantic["case_id"]
                    or prior_semantic.get("work_ref") != semantic["work_ref"]
                    or type(prior_semantic.get("analysis_generation")) is not int
                    or prior_semantic["analysis_generation"] >= semantic["analysis_generation"]
                ):
                    raise click.ClickException(
                        "supersedes_conflict: prior event must be a lower-generation observation for the same case and work"
                    )
            matches = db.find_semantic_observations_by_key(conn, interpretation_key)
            if matches:
                conn.rollback()
                if len(matches) != 1:
                    raise click.ClickException(
                        "interpretation_key_conflict: multiple local observations already use this key"
                    )
                existing = matches[0]
                candidate_digest = hashlib.sha256(
                    canonical_payload_json(observation_payload(event)).encode("utf-8")
                ).hexdigest()
                existing_digest = hashlib.sha256(
                    canonical_payload_json(observation_payload(existing)).encode("utf-8")
                ).hexdigest()
                if existing_digest != candidate_digest:
                    raise click.ClickException(
                        "interpretation_key_conflict: key already has different content; use a new analysis_generation"
                    )
                return existing, context, context.shard_for(existing["ts"]), True

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
        return event, context, ndjson_path, False
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()


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
    if type_ == "semantic.observation" or source == "semantic-consumer":
        raise click.ClickException("semantic.observation must use semantic-replay")
    try:
        timestamp = validate_timestamp(ts or _now())
        event = {
            "ts": timestamp,
            "type": type_,
            "actor": actor,
            "summary": summary,
            "detail": detail,
            "refs": validate_refs(list(refs)),
            "source": source,
            "metadata": parse_metadata(metadata),
        }
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        event, context, ndjson_path, _duplicate = _record_event(event)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

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


@cli.command("semantic-replay")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Frozen Auditctl source snapshot manifest",
)
@click.option(
    "--manifest-sha256",
    required=True,
    help="Externally recorded lowercase SHA-256 of the exact manifest bytes",
)
@click.option(
    "--result",
    "result_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Precomputed semantic-observation/v1 result from an out-of-process consumer",
)
@click.option(
    "--result-sha256",
    required=True,
    help="Externally recorded lowercase SHA-256 of the exact result bytes",
)
def semantic_replay_cmd(
    manifest_path: Path, manifest_sha256: str, result_path: Path, result_sha256: str
) -> None:
    """Verify and append one asynchronous semantic result against frozen sources."""
    from .semantic import build_event, interpretation_key, read_result, validate_result
    from .source_preflight import manifest_source_paths, preflight

    try:
        basis = preflight(manifest_path, manifest_sha256)
        source_paths = manifest_source_paths(manifest_path, manifest_sha256)
        result, actual_result_sha256 = read_result(result_path, result_sha256)
        result = validate_result(result, basis["manifest_sha256"])
        key = interpretation_key(result)
        event, context, ndjson_path, duplicate = _record_event(
            build_event(result, actual_result_sha256),
            interpretation_key=key,
            immutable_input_paths=(manifest_path, result_path, *source_paths),
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            {
                "event_id": event["event_id"],
                "origin_stream_id": event["origin_stream_id"],
                "origin_seq": event["origin_seq"],
                "interpretation_key": key,
                "source_basis_ref": result["source_basis_ref"],
                "source_basis_sha256": result["source_basis_sha256"],
                "source_manifest_sha256": basis["manifest_sha256"],
                "result_sha256": actual_result_sha256,
                "basis_qualification": basis["qualification"],
                "duplicate": duplicate,
                "ndjson_path": str(ndjson_path),
                "repo_id": context.repo_id,
            },
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    )


@cli.command("list")
@click.option("--type", "type_", default=None, help="Filter by event type")
@click.option("--source", default=None, help="Filter by source")
@click.option("--since", default=None, help="Inclusive ISO UTC lower bound")
@click.option("--until", default=None, help="Inclusive ISO UTC upper bound")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "output_json", is_flag=True, default=False)
@click.option("--attribution-overlay", type=click.Path(path_type=Path), multiple=True,
              help="Explicit verified attribution overlay; repeat for protected tails")
@click.option("--attribution-source", multiple=True, metavar="SOURCE_ID=PATH",
              help="Explicit frozen source snapshot; repeat for verified copies")
@click.option("--attributed-repo", default=None, help="Filter explicitly mapped repository in snapshot mode")
def list_cmd(type_, source, since, until, limit, output_json, attribution_overlay,
             attribution_source, attributed_repo) -> None:
    """List audit events newest first."""
    if attribution_overlay or attribution_source or attributed_repo is not None:
        from .attribution_listing import AttributionListingError, list_snapshots

        try:
            if not output_json:
                raise AttributionListingError("snapshot attribution requires --json")
            if since:
                validate_timestamp(since)
            if until:
                validate_timestamp(until)
            report = list_snapshots(attribution_overlay, attribution_source,
                                    attributed_repo=attributed_repo, type_=type_, source=source,
                                    since=since, until=until, limit=limit)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(report, sort_keys=True))
        return
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
