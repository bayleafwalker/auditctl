"""The evidence-ledger falsifier, as settled on 2026-08-30.

The owner settled that auditctl is the canonical home of `EvidenceSet` and `Decision`,
and bound the settlement to a falsifier rather than to a promise
(`vuoro:docs/plans/2026-08-22-long-term-direction.md` section 14):

    Falsifier: an evidence artifact that no ledger id resolves to, or a decision
    reachable only by reading a file in a working tree.

The consequence recorded beside it is the operative rule: every artifact that today
improvises whether it is a record or a build output -- `verification/results/` and the
acceptance lab's `campaigns/` are the two named -- is a **derived build output that cites
a ledger id**. A build output may be regenerated, pruned, or lost. A record may not. What
makes the first safe is that the record it cites lives somewhere else, and that the
citation resolves.

So this module is a falsifier and not a validator. It asks one question of each evidence
artifact -- *does a ledger id resolve to it* -- and reports every file for which the
answer is no. Two things follow from that framing:

**It is expected to fail today.** Nothing in either named directory carries a ledger id,
because the convention post-dates every file in them. A failing run is the finding the
settlement asked for; it is not a defect in the check. The report therefore lists the
offending paths and says which limb of the falsifier each one trips, because a check that
only returns a count cannot be acted on.

**It never repairs.** There is no `--fix`, no id minting, and no write path. Stamping a
ledger id into an artifact would satisfy the text of the falsifier while destroying the
property it protects: the id would resolve to nothing, or worse, to an event invented to
make it resolve. The remedy is to record the evidence in the ledger and have the producing
build cite the id it got back.

## What counts as an evidence artifact

Declared, not guessed. Guessing invites the same improvisation the settlement removes:

- any file under a `verification/results/` directory;
- any file under a `campaigns/<name>/` directory;
- any JSON file whose top-level `schema_version` names a known evidence schema.

Directory shapes are matched anywhere beneath the given root, so this works against a
single repository or a whole workspace.

## What counts as a ledger id

An auditctl event id: `ad:` followed by 26 Crockford base32 characters, the form
`validation.EVENT_ID_RE` accepts. An artifact cites one by carrying it anywhere in its
bytes -- in a dedicated `ledger_id` field, in a `refs` list, or in prose. Being liberal
about *where* the citation sits and strict about whether it *resolves* is deliberate: the
falsifier is about the ledger link existing, not about a field name nobody has agreed on
yet.

Resolution is checked against real stores -- sqlite indexes and NDJSON shards -- so an id
that is well-formed but names no event is reported as loudly as no id at all. That case is
worse than a missing citation, not better, because it looks compliant.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .validation import EVENT_ID_RE

#: A ledger id as it appears inside an artifact. Anchored `EVENT_ID_RE` matches a whole
#: string; this one finds the same form embedded in a larger document.
LEDGER_ID_RE = re.compile(r"ad:[0-9A-HJKMNP-TV-Z]{26}")

#: Directory names whose contents the settlement names explicitly.
EVIDENCE_DIRECTORIES = (
    ("verification", "results"),
    ("campaigns",),
)

#: Top-level `schema_version` prefixes that declare a file to be evidence wherever it sits.
EVIDENCE_SCHEMA_PREFIXES = (
    "verification-result/",
    "evidence-set/",
    "decision/",
)

#: Fields whose presence means the file states a judgement rather than only an observation.
#: A judgement that exists only here is the falsifier's second limb.
DECISION_MARKER_FIELDS = (
    "claims",
    "counterexamples",
    "status",
    "aggregate_score",
    "verdict",
    "decision",
    "outcome",
)

#: Why one artifact falsifies the settlement. Closed, so a report cannot invent a reason.
NO_LEDGER_ID = "no_ledger_id"
UNRESOLVABLE_LEDGER_ID = "unresolvable_ledger_id"
DECISION_ONLY_IN_WORKING_TREE = "decision_only_in_working_tree"

_REASON_TEXT = {
    NO_LEDGER_ID: "cites no ledger id",
    UNRESOLVABLE_LEDGER_ID: "cites ledger id(s) that resolve to no event",
    DECISION_ONLY_IN_WORKING_TREE: (
        "states a judgement that no ledger decision record resolves, so the decision is "
        "reachable only by reading this file"
    ),
}


@dataclass(frozen=True)
class LedgerIndex:
    """Which ids the ledger holds, and which of those are decisions.

    Built once and queried per artifact. The alternative -- reopening a store for every
    citation -- turns a scan of two directories into thousands of connections.
    """

    ids: frozenset[str] = frozenset()
    decision_ids: frozenset[str] = frozenset()
    stores: tuple[Path, ...] = ()
    unreadable: tuple[str, ...] = ()

    def holds(self, ledger_id: str) -> bool:
        return ledger_id in self.ids

    def holds_decision(self, ledger_id: str) -> bool:
        return ledger_id in self.decision_ids


@dataclass(frozen=True)
class EvidenceArtifact:
    """One file the settlement classes as evidence, and what it cites."""

    path: Path
    kind: str
    cited_ids: tuple[str, ...] = ()
    states_judgement: bool = False


@dataclass(frozen=True)
class Offender:
    """One artifact that falsifies the settlement, and which limb it trips."""

    path: Path
    kind: str
    reason: str
    cited_ids: tuple[str, ...] = ()

    @property
    def explanation(self) -> str:
        return _REASON_TEXT[self.reason]


@dataclass(frozen=True)
class FalsifierReport:
    artifacts: tuple[EvidenceArtifact, ...] = ()
    offenders: tuple[Offender, ...] = ()
    ledger: LedgerIndex = field(default_factory=LedgerIndex)
    roots: tuple[Path, ...] = ()

    @property
    def falsified(self) -> bool:
        """True when the settlement's falsifier fires against this tree."""
        return bool(self.offenders)

    def as_record(self) -> dict[str, Any]:
        return {
            "falsified": self.falsified,
            "roots": [str(path) for path in self.roots],
            "artifacts_examined": len(self.artifacts),
            "offenders": [
                {
                    "path": str(offender.path),
                    "kind": offender.kind,
                    "reason": offender.reason,
                    "explanation": offender.explanation,
                    "cited_ids": list(offender.cited_ids),
                }
                for offender in self.offenders
            ],
            "offenders_by_reason": {
                reason: sum(1 for o in self.offenders if o.reason == reason)
                for reason in (NO_LEDGER_ID, UNRESOLVABLE_LEDGER_ID, DECISION_ONLY_IN_WORKING_TREE)
            },
            "ledger": {
                "stores": [str(path) for path in self.ledger.stores],
                "events": len(self.ledger.ids),
                "decisions": len(self.ledger.decision_ids),
                "unreadable": list(self.ledger.unreadable),
            },
        }


def build_ledger_index(
    *, indexes: Iterable[Path] = (), shards: Iterable[Path] = ()
) -> LedgerIndex:
    """Read the ids the ledger holds. Read-only; opens sqlite in `mode=ro`."""

    ids: set[str] = set()
    decisions: set[str] = set()
    stores: list[Path] = []
    unreadable: list[str] = []

    for path in indexes:
        path = Path(path)
        stores.append(path)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            unreadable.append(f"{path}: {exc}")
            continue
        try:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM audit_event").fetchall()
            except sqlite3.Error as exc:
                unreadable.append(f"{path}: {exc}")
                continue
        finally:
            conn.close()
        for row in rows:
            event_id = row["id"]
            if not event_id:
                continue
            ids.add(event_id)
            # `record_class` arrived in a migration, so a store that predates it has no
            # such column. Absent means observation, which is its documented default.
            try:
                record_class = row["record_class"]
            except (IndexError, KeyError):
                record_class = None
            if record_class == "decision":
                decisions.add(event_id)

    for path in shards:
        path = Path(path)
        stores.append(path)
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            unreadable.append(f"{path}: {exc}")
            continue
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
                if not isinstance(event, dict):
                    unreadable.append(f"{path}:{lineno}: not an event object")
                    continue
                event_id = event.get("id") or event.get("event_id")
                if isinstance(event_id, str) and event_id:
                    ids.add(event_id)
                    if event.get("record_class") == "decision":
                        decisions.add(event_id)

    return LedgerIndex(
        ids=frozenset(ids),
        decision_ids=frozenset(decisions),
        stores=tuple(stores),
        unreadable=tuple(unreadable),
    )


def _classify(path: Path) -> str | None:
    """Which declared evidence shape this path belongs to, or None.

    Matched against the *whole* path rather than the part below the scan root. Matching
    the relative remainder made the answer depend on where the caller pointed: scanning
    `/projects/dev/acceptance-lab` classified the campaign artifacts and scanning
    `/projects/dev/acceptance-lab/campaigns` classified none of them, because the
    directory that names them had been consumed by the root. A falsifier whose verdict
    moves with the caller's convenience is not a falsifier.
    """
    parts = path.parts
    for shape in EVIDENCE_DIRECTORIES:
        window = len(shape)
        for start in range(len(parts) - window):
            if parts[start : start + window] == shape:
                return "/".join(shape)
    return None


def _declared_schema(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    schema = payload.get("schema_version")
    if not isinstance(schema, str):
        return None
    for prefix in EVIDENCE_SCHEMA_PREFIXES:
        if schema.startswith(prefix):
            return schema
    return None


def _states_judgement(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(marker in payload for marker in DECISION_MARKER_FIELDS)


def discover_evidence_artifacts(roots: Iterable[Path]) -> list[EvidenceArtifact]:
    """Every file beneath the roots that the settlement classes as evidence."""

    found: dict[str, EvidenceArtifact] = {}
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            payload: Any = None
            if path.suffix == ".json":
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    payload = None
            kind = _classify(path)
            schema = _declared_schema(payload)
            if kind is None and schema is None:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cited = tuple(sorted(set(LEDGER_ID_RE.findall(text))))
            found[str(path)] = EvidenceArtifact(
                path=path,
                kind=kind or schema or "evidence",
                cited_ids=cited,
                states_judgement=_states_judgement(payload),
            )
    return list(found.values())


def falsify(
    roots: Iterable[Path],
    ledger: LedgerIndex | None = None,
) -> FalsifierReport:
    """Run the settled falsifier over an artifacts root.

    Reports every evidence artifact that no ledger id resolves to, and every judgement
    that exists only as a file in a working tree. Never writes anything.
    """

    root_paths = tuple(Path(root).expanduser() for root in roots)
    ledger = ledger if ledger is not None else LedgerIndex()
    artifacts = tuple(discover_evidence_artifacts(root_paths))

    offenders: list[Offender] = []
    for artifact in artifacts:
        resolved = [i for i in artifact.cited_ids if ledger.holds(i)]
        if not artifact.cited_ids:
            offenders.append(
                Offender(artifact.path, artifact.kind, NO_LEDGER_ID, artifact.cited_ids)
            )
            continue
        if not resolved:
            # Worse than silence: it reads as compliant and is not.
            offenders.append(
                Offender(
                    artifact.path, artifact.kind, UNRESOLVABLE_LEDGER_ID, artifact.cited_ids
                )
            )
            continue
        if artifact.states_judgement and not any(ledger.holds_decision(i) for i in resolved):
            offenders.append(
                Offender(
                    artifact.path,
                    artifact.kind,
                    DECISION_ONLY_IN_WORKING_TREE,
                    tuple(resolved),
                )
            )

    offenders.sort(key=lambda o: (o.reason, str(o.path)))
    return FalsifierReport(
        artifacts=artifacts,
        offenders=tuple(offenders),
        ledger=ledger,
        roots=root_paths,
    )


def render_report(report: FalsifierReport) -> str:
    """The finding, with the paths. A count on its own cannot be acted on."""

    lines = [
        f"Evidence-ledger falsifier (settled 2026-08-30): examined "
        f"{len(report.artifacts)} evidence artifact(s) under "
        f"{', '.join(str(root) for root in report.roots) or '(no root)'}",
        f"Ledger: {len(report.ledger.ids)} event(s) "
        f"({len(report.ledger.decision_ids)} decision record(s)) "
        f"across {len(report.ledger.stores)} store(s)",
        "",
    ]
    if not report.artifacts:
        lines.append("No evidence artifacts found. The falsifier has nothing to test.")
        return "\n".join(lines)
    if not report.offenders:
        lines.append("NOT FALSIFIED: every evidence artifact resolves to a ledger id.")
        return "\n".join(lines)

    lines.append(
        f"FALSIFIED: {len(report.offenders)} of {len(report.artifacts)} evidence "
        f"artifact(s) are not reachable from the ledger."
    )
    for reason in (NO_LEDGER_ID, UNRESOLVABLE_LEDGER_ID, DECISION_ONLY_IN_WORKING_TREE):
        group = [o for o in report.offenders if o.reason == reason]
        if not group:
            continue
        lines.append("")
        lines.append(f"{reason} -- {_REASON_TEXT[reason]} ({len(group)}):")
        for offender in group:
            cited = f"  [cited: {', '.join(offender.cited_ids)}]" if offender.cited_ids else ""
            lines.append(f"  {offender.path}  ({offender.kind}){cited}")
    lines.append("")
    lines.append(
        "This is the finding the settlement asked for, not a defect in the check. The "
        "remedy is to record each artifact's evidence in the ledger and have the "
        "producing build cite the id it received. Do not stamp an id into a file to "
        "silence this: an id that resolves to nothing is the worse of the two shapes, and "
        "an event invented to make it resolve stops the ledger being evidence at all."
    )
    if report.ledger.unreadable:
        lines.append("")
        lines.append(f"{len(report.ledger.unreadable)} ledger store(s)/line(s) unreadable:")
        lines.extend(f"  {entry}" for entry in report.ledger.unreadable[:10])
    return "\n".join(lines)


def is_ledger_id(value: str) -> bool:
    """Whether a string is a whole, well-formed ledger id."""
    return bool(EVENT_ID_RE.match(value))
