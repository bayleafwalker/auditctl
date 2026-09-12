"""What the declared terminal-reason vocabulary has actually been used for.

`TERMINAL_REASON_CODES` in `validation.py` is a *closed* vocabulary: an unlisted code is
rejected at the write path. A closed vocabulary is only as good as its coverage, and
nothing in this repository could say what that coverage was -- the validator answers
"is this code allowed", never "has this code ever been written". Those are different
questions and only the second one finds a code the fleet cannot actually produce.

This module answers the second one by counting, and by counting only. It reads existing
stores and never writes one. It does not emit, backfill, or synthesise an event to close
a gap it finds: a ledger that manufactures the observations it was supposed to be
measuring has stopped being evidence of anything.

Two properties of the count are load-bearing and easy to get wrong:

*Deduplicate by event id.* Every `auditctl add` lands the same event in the sqlite index
*and* in an NDJSON shard, by construction. Summing a union of both stores therefore
double-counts every event whose shard is still on disk -- measured on the `/projects/dev`
workspace on 2026-09-12, that inflated the bearing-event total from 1997 to 2059. A
coverage number that moves when a shard is pruned is not a coverage number.

*Read leniently.* `ndjson.read_events` validates, and aborts the whole file on the first
event today's validator refuses. The reporter must not: a historical event that predates
a tightening is still a record of something that was observed, and dropping a shard
because its ninth line is old would understate exactly the codes most likely to be rare.
Lines and rows that cannot be parsed at all are counted and reported, not silently
skipped.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .validation import TERMINAL_REASON_CODES

#: A repo-local index, relative to the repository root.
INDEX_RELATIVE_PATH = Path(".auditctl") / "auditctl.db"
#: Shard layout beneath a workspace root, mirroring `paths.shard_path`.
SHARD_GLOB = "_artifacts/*/audit/events-*.ndjson"


@dataclass(frozen=True)
class TerminalReasonRow:
    """One declared code and what the scanned evidence says about it."""

    code: str
    count: int
    first_ts: str | None = None
    last_ts: str | None = None

    @property
    def written(self) -> bool:
        return self.count > 0


@dataclass(frozen=True)
class TerminalReasonCoverage:
    """The vocabulary, the observed counts, and what was read to get them."""

    rows: tuple[TerminalReasonRow, ...]
    indexes: tuple[Path, ...] = ()
    shards: tuple[Path, ...] = ()
    events_scanned: int = 0
    bearing_events: int = 0
    off_vocabulary: Mapping[str, int] | None = None
    unreadable: tuple[str, ...] = ()

    @property
    def unwritten(self) -> tuple[str, ...]:
        """Declared codes that no scanned store has ever carried."""
        return tuple(row.code for row in self.rows if not row.written)

    @property
    def written(self) -> tuple[str, ...]:
        return tuple(row.code for row in self.rows if row.written)

    @property
    def first_ts(self) -> str | None:
        stamps = [row.first_ts for row in self.rows if row.first_ts]
        return min(stamps) if stamps else None

    @property
    def last_ts(self) -> str | None:
        stamps = [row.last_ts for row in self.rows if row.last_ts]
        return max(stamps) if stamps else None

    def as_record(self) -> dict[str, Any]:
        return {
            "vocabulary_size": len(self.rows),
            "codes": [
                {
                    "code": row.code,
                    "count": row.count,
                    "written": row.written,
                    "first_ts": row.first_ts,
                    "last_ts": row.last_ts,
                }
                for row in self.rows
            ],
            "unwritten": list(self.unwritten),
            "indexes_scanned": [str(path) for path in self.indexes],
            "shards_scanned": [str(path) for path in self.shards],
            "events_scanned": self.events_scanned,
            "bearing_events": self.bearing_events,
            "observed_range": {"first_ts": self.first_ts, "last_ts": self.last_ts},
            "off_vocabulary": dict(self.off_vocabulary or {}),
            "unreadable": list(self.unreadable),
        }


def discover_stores(roots: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    """Every index and shard beneath the given workspace roots.

    A root may be a workspace holding many repositories (`/projects/dev`) or a single
    repository. Both shapes are checked, because the shard tree and the index tree are
    not siblings: shards pool under `<root>/_artifacts/<repo_id>/audit/` while each
    index stays in its own repository at `<repo>/.auditctl/auditctl.db`.
    """

    indexes: list[Path] = []
    shards: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        own_index = root / INDEX_RELATIVE_PATH
        if own_index.is_file():
            indexes.append(own_index)
        for child in sorted(root.glob(f"*/{INDEX_RELATIVE_PATH.as_posix()}")):
            if child.is_file():
                indexes.append(child)
        shards.extend(sorted(root.glob(SHARD_GLOB)))
    return _unique(indexes), _unique(shards)


def _unique(paths: Iterable[Path]) -> list[Path]:
    seen: dict[str, Path] = {}
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        seen.setdefault(key, path)
    return list(seen.values())


def _metadata(raw: Any) -> dict[str, Any] | None:
    """A metadata mapping, whether it arrived as an object or as stored JSON text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw if isinstance(raw, dict) else None


def _terminal_reason(event: Mapping[str, Any]) -> str | None:
    metadata = _metadata(event.get("metadata"))
    if metadata is None:
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            metadata = _metadata(payload.get("metadata"))
    if metadata is None:
        return None
    value = metadata.get("terminal_reason")
    # A non-string value is not a reason code. The publisher contract's own regression
    # fixtures carry `terminal_reason` as a dict, so this is a real historical shape.
    return value if isinstance(value, str) and value else None


def _read_index(path: Path, unreadable: list[str]) -> Iterator[dict[str, Any]]:
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        unreadable.append(f"{path}: {exc}")
        return
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, ts, metadata FROM audit_event").fetchall()
        except sqlite3.Error as exc:
            unreadable.append(f"{path}: {exc}")
            return
    finally:
        conn.close()
    for row in rows:
        yield {"id": row["id"], "ts": row["ts"], "metadata": row["metadata"]}


def _read_shard(path: Path, unreadable: list[str]) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        unreadable.append(f"{path}: {exc}")
        return
    with handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except (UnicodeError, ValueError):
                unreadable.append(f"{path}:{lineno}: unparseable line")
                continue
            if isinstance(event, dict):
                yield event
            else:
                unreadable.append(f"{path}:{lineno}: not an event object")


def measure_terminal_reasons(
    *,
    indexes: Iterable[Path] = (),
    shards: Iterable[Path] = (),
    vocabulary: Iterable[str] = TERMINAL_REASON_CODES,
) -> TerminalReasonCoverage:
    """Count each declared terminal reason across the given stores.

    Read-only. Never writes, emits, or infers an event.
    """

    index_paths = _unique(indexes)
    shard_paths = _unique(shards)
    unreadable: list[str] = []

    # id -> (code, ts). One entry per event, so an event present in both its index and
    # its shard is counted once. Events without an id cannot be deduplicated and are
    # keyed by their position instead, which is the honest fallback: a store that lost
    # ids has lost the ability to tell a duplicate from a repetition.
    seen: dict[str, tuple[str, str | None]] = {}
    events_scanned = 0
    for source in (
        *(_read_index(path, unreadable) for path in index_paths),
        *(_read_shard(path, unreadable) for path in shard_paths),
    ):
        for event in source:
            events_scanned += 1
            code = _terminal_reason(event)
            if code is None:
                continue
            key = event.get("id") or event.get("event_id")
            if not isinstance(key, str) or not key:
                key = f"__anonymous__:{events_scanned}"
            ts = event.get("ts") or event.get("occurred_at")
            seen.setdefault(key, (code, ts if isinstance(ts, str) else None))

    counts: Counter[str] = Counter()
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for code, ts in seen.values():
        counts[code] += 1
        if ts:
            first[code] = min(first.get(code, ts), ts)
            last[code] = max(last.get(code, ts), ts)

    declared = sorted(vocabulary)
    rows = tuple(
        TerminalReasonRow(
            code=code,
            count=counts.get(code, 0),
            first_ts=first.get(code),
            last_ts=last.get(code),
        )
        for code in declared
    )
    off_vocabulary = {
        code: count for code, count in sorted(counts.items()) if code not in set(declared)
    }
    return TerminalReasonCoverage(
        rows=rows,
        indexes=tuple(index_paths),
        shards=tuple(shard_paths),
        events_scanned=events_scanned,
        bearing_events=len(seen),
        off_vocabulary=off_vocabulary,
        unreadable=tuple(unreadable),
    )


def render_terminal_reason_coverage(coverage: TerminalReasonCoverage) -> str:
    """A report that names the gap rather than only scoring it."""

    lines = [
        f"{'TERMINAL REASON':18}  {'COUNT':>7}  {'FIRST SEEN':10}  {'LAST SEEN':10}  STATUS",
    ]
    for row in coverage.rows:
        status = "observed" if row.written else "NEVER WRITTEN"
        # Dates, not full timestamps: the question this answers is "has anything ever
        # produced this code", for which the day is the useful resolution. The exact
        # stamps are in --json.
        first = (row.first_ts or "-")[:10]
        last = (row.last_ts or "-")[:10]
        lines.append(f"{row.code:18}  {row.count:7}  {first:10}  {last:10}  {status}")
    lines.append("")
    lines.append(
        f"Scanned {len(coverage.indexes)} index(es) and {len(coverage.shards)} shard(s): "
        f"{coverage.events_scanned} row(s)/line(s) read, "
        f"{coverage.bearing_events} distinct event(s) carrying a terminal_reason."
    )
    if coverage.first_ts:
        lines.append(f"Observed range: {coverage.first_ts} .. {coverage.last_ts}")
    if coverage.off_vocabulary:
        rendered = ", ".join(f"{code}={count}" for code, count in coverage.off_vocabulary.items())
        lines.append(f"Off-vocabulary codes present in evidence: {rendered}")
    if coverage.unwritten:
        lines.append(
            f"{len(coverage.unwritten)} of {len(coverage.rows)} declared code(s) have never "
            f"been written: {', '.join(coverage.unwritten)}"
        )
        lines.append(
            "A declared code with no observation is an unexercised contract branch, not a "
            "hole to be filled: find the publisher that should emit it, or retire the code. "
            "Do not write events to close this gap."
        )
    else:
        lines.append("Every declared terminal reason has at least one observation.")
    if coverage.unreadable:
        lines.append(f"{len(coverage.unreadable)} store(s)/line(s) could not be read:")
        lines.extend(f"  {entry}" for entry in coverage.unreadable[:10])
        if len(coverage.unreadable) > 10:
            lines.append(f"  ... and {len(coverage.unreadable) - 10} more")
    return "\n".join(lines)
