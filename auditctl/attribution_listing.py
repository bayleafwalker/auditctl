"""Read-only snapshot reporting through the installed Agentops CLI.

This module validates a consumer interface, not source attribution authority.
The canonical source/digest verifier remains in Agentops.
"""
from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import stat
import shutil
import subprocess
import tempfile
import time


class AttributionListingError(ValueError):
    pass


_RESULT_FIELDS = frozenset({
    "schema_version", "overlays", "coverage", "verified_source_count",
    "verified_copy_count", "unparsed_bytes_beyond_snapshots", "unique_events",
    "duplicate_records", "mapped_events", "unmapped_events", "attributed_counts", "events",
})
_RECORD_FIELDS = frozenset({"event_id", "source_repo_id", "attributed_repo_id", "mapped",
                            "raw_line_sha256", "original_event"})
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fail(message):
    raise AttributionListingError(message)


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            _fail("duplicate field in attribution interface")
        obj[key] = value
    return obj


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        _fail("nonfinite attribution interface")
    return number


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8") if type(raw) is bytes else raw, object_pairs_hook=_object, parse_float=_float,
                          parse_constant=lambda _: _fail("nonfinite attribution interface"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AttributionListingError("malformed attribution interface") from error


def _count(value):
    if type(value) is not int or value < 0:
        _fail("invalid attribution interface count")


def _token(value):
    if type(value) is not str or not _TOKEN.fullmatch(value):
        _fail("invalid attribution interface repository")


def _regular_file(path, *, read=False):
    descriptor = None
    try:
        # NONBLOCK avoids opening a FIFO/device indefinitely before eligibility
        # can be measured. Ordinary symlinks to regular files are supported.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            _fail("attribution inputs must be regular files")
        if not read:
            return info.st_size
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except OSError as error:
        raise AttributionListingError("attribution input is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _timeout_seconds():
    try:
        value = float(os.environ.get("AUDITCTL_ATTRIBUTION_TIMEOUT_SECONDS", "300"))
    except ValueError as error:
        raise AttributionListingError("invalid attribution deadline configuration") from error
    if not math.isfinite(value) or not 0 < value <= 3600:
        _fail("attribution deadline must be positive and at most 3600 seconds")
    return value


def _output_budget(overlays, sources):
    """Conservative serialization bound; this never verifies a manifest.

    The v1 helper uses json.dumps without indent; pretty-print expansion is
    outside this consumer interface. Original JSON can expand UTF-8 into
    six-byte ASCII escapes. Eight
    times physical source bytes also covers the repeated event ID. Each declared
    source line reserves fixed record-wrapper overhead and the largest declared
    repository tokens. Overlay and argv bytes bound receipt metadata. Semantic
    validation, including all snapshot counts, remains the helper's job.
    """
    source_bytes = 0
    for source in sources:
        identity, separator, filename = source.partition("=")
        if not separator or not identity or not filename:
            _fail("attribution sources must be SOURCE_ID=PATH")
        source_bytes += _regular_file(filename)
    overlay_bytes, lines, namespace_bytes = 0, 0, 0
    try:
        for filename in overlays:
            raw = _regular_file(filename, read=True)
            overlay_bytes += len(raw)
            value = _parse(raw)
            if type(value) is not dict or type(value.get("sources")) is not list or type(value.get("entries")) is not list:
                _fail("attribution overlay cannot establish an output budget")
            for source in value["sources"]:
                if type(source) is not dict:
                    _fail("invalid attribution source descriptor")
                count = source.get("line_count")
                _count(count)
                # A valid nonempty NDJSON line consumes at least one byte.
                if count > source_bytes:
                    _fail("attribution source count exceeds available byte budget")
                lines += count
                _token(source.get("source_repo_id"))
                namespace_bytes = max(namespace_bytes, len(json.dumps(source["source_repo_id"])))
            for entry in value["entries"]:
                if type(entry) is not dict:
                    _fail("invalid attribution entry descriptor")
                _token(entry.get("attributed_repo_id"))
                namespace_bytes = max(namespace_bytes, len(json.dumps(entry["attributed_repo_id"])))
    except OSError as error:
        raise AttributionListingError("attribution overlay is unavailable") from error
    argv_bytes = sum(len(str(value).encode("utf-8")) for value in [*overlays, *sources])
    return 8 * source_bytes + lines * (384 + 2 * namespace_bytes) + 8 * overlay_bytes + 8 * argv_bytes + 65536


def _invoke(overlays, sources, byte_limit):
    executable = shutil.which("agentops")
    if executable is None:
        _fail("installed agentops command is required for snapshot attribution")
    command = [executable, "query-audit-attribution", "--events"]
    for overlay in overlays:
        command += ["--overlay", str(overlay)]
    for source in sources:
        command += ["--source", source]
    deadline = time.monotonic() + _timeout_seconds()
    try:
        with tempfile.TemporaryFile(mode="w+b") as spool:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  start_new_session=True) as child:
                completed = False
                try:
                    used = 0
                    with selectors.DefaultSelector() as selector:
                        selector.register(child.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                _fail("attribution helper exceeded reporting deadline")
                            chunk = child.stdout.read1(65536)
                            if not chunk:
                                break
                            used += len(chunk)
                            if used > byte_limit:
                                _fail("attribution helper response exceeds derived byte budget")
                            spool.write(chunk)
                    try:
                        status = child.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        _fail("attribution helper exceeded reporting deadline")
                    if status != 0:
                        _fail("attribution helper could not verify the supplied snapshots")
                    spool.seek(0)
                    result = _parse(spool.read())
                    completed = True
                    return result
                finally:
                    if not completed:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
    except OSError as error:
        raise AttributionListingError("attribution command or response spool is unavailable") from error


def _validate(result):
    if type(result) is not dict or result.keys() != _RESULT_FIELDS or result.get("schema_version") != "audit-attribution-query/v1":
        _fail("unsupported attribution helper interface")
    coverage = result["coverage"]
    if coverage not in ("committed-only", "committed-and-explicit-protected-tail"):
        _fail("unsupported attribution coverage")
    for field in ("verified_source_count", "verified_copy_count", "unique_events", "duplicate_records", "mapped_events", "unmapped_events"):
        _count(result[field])
    if type(result["overlays"]) is not list or not result["overlays"]:
        _fail("invalid attribution overlay receipt")
    tail = False
    for overlay in result["overlays"]:
        if type(overlay) is not dict or overlay.keys() != {"scope", "digest", "mapped_events"}:
            _fail("invalid attribution overlay receipt")
        if overlay["scope"] not in ("committed-only", "protected-tail") or type(overlay["digest"]) is not str or not _DIGEST.fullmatch(overlay["digest"]):
            _fail("invalid attribution overlay revision")
        _count(overlay["mapped_events"])
        tail |= overlay["scope"] == "protected-tail"
    if tail != (coverage == "committed-and-explicit-protected-tail"):
        _fail("inconsistent attribution coverage")
    for field in ("unparsed_bytes_beyond_snapshots", "attributed_counts"):
        if type(result[field]) is not dict:
            _fail("invalid attribution count map")
        for key, value in result[field].items():
            _token(key)
            if field == "unparsed_bytes_beyond_snapshots":
                if type(value) is not bool:
                    _fail("invalid attribution append-extension flag")
            else:
                _count(value)
    if result["verified_source_count"] < 1 or result["verified_copy_count"] < result["verified_source_count"] or len(result["unparsed_bytes_beyond_snapshots"]) != result["verified_source_count"]:
        _fail("inconsistent attribution source receipt")
    if sum(overlay["mapped_events"] for overlay in result["overlays"]) != result["mapped_events"]:
        _fail("inconsistent attribution overlay counts")
    if type(result["events"]) is not list or len(result["events"]) != result["unique_events"]:
        _fail("incomplete attribution event interface")
    seen, counts = set(), {}
    for record in result["events"]:
        if type(record) is not dict or record.keys() != _RECORD_FIELDS:
            _fail("unknown attribution event fields")
        event = record["original_event"]
        if type(event) is not dict or type(record["event_id"]) is not str or not record["event_id"] or event.get("id") != record["event_id"] or record["event_id"] in seen:
            _fail("invalid attribution event identity")
        seen.add(record["event_id"])
        for field in ("source_repo_id", "attributed_repo_id"):
            _token(record[field])
        if type(record["mapped"]) is not bool or type(record["raw_line_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", record["raw_line_sha256"]):
            _fail("invalid attribution event binding")
        # Only these fields are needed for ordinary list filtering/order. All
        # original data, including unknown historical fields, remains intact.
        for field in ("ts", "type", "source"):
            if type(event.get(field)) is not str:
                _fail("original event lacks list fields")
        if record["mapped"]:
            repo = record["attributed_repo_id"]
            counts[repo] = counts.get(repo, 0) + 1
        elif record["attributed_repo_id"] != record["source_repo_id"]:
            _fail("unmapped attribution changed original repository")
    if counts != result["attributed_counts"] or sum(counts.values()) != result["mapped_events"] or result["mapped_events"] + result["unmapped_events"] != result["unique_events"]:
        _fail("inconsistent attribution event counts")
    return result


def list_snapshots(overlays, sources, *, attributed_repo=None, type_=None, source=None,
                   since=None, until=None, limit=50):
    if not overlays or not sources:
        _fail("snapshot attribution requires overlays and explicit sources")
    if limit < 0:
        _fail("snapshot attribution limit must be nonnegative")
    if attributed_repo is not None:
        _token(attributed_repo)
    result = _validate(_invoke(overlays, sources, _output_budget(overlays, sources)))
    matching = []
    for record in result["events"]:
        event = record["original_event"]
        if attributed_repo is not None and (not record["mapped"] or record["attributed_repo_id"] != attributed_repo):
            continue
        if type_ is not None and event["type"] != type_ or source is not None and event["source"] != source:
            continue
        if since is not None and event["ts"] < since or until is not None and event["ts"] > until:
            continue
        matching.append(record)
    matching.sort(key=lambda record: (record["original_event"]["ts"], record["event_id"]), reverse=True)
    return {"schema_version": "auditctl-list-attribution/v1", "mode": "verified-source-snapshots",
            "verification_receipt": {key: value for key, value in result.items() if key != "events"},
            "filters": {"attributed_repo": attributed_repo, "type": type_, "source": source,
                        "since": since, "until": until, "limit": limit},
            "matched_events": len(matching), "returned_events": min(limit, len(matching)),
            "events": [{"original_event": record["original_event"],
                        "attribution": {key: value for key, value in record.items() if key != "original_event"}}
                       for record in matching[:limit]]}
