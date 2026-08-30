import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CENTRAL_CONTEXT_ID = "auditctl.central-observation-ingest"
CENTRAL_IMPLEMENTATION_PATHS = (
    "auditctl.dispatch.json",
    "auditctl/central.py",
    "auditctl/central_migrations/__init__.py",
    "auditctl/central_migrations/versions/0001_ingest.sql",
    "auditctl/central_migrations/versions/0002_receipts_and_reads.sql",
    "auditctl/central_schema.py",
    "pyproject.toml",
)
VUORO_ADAPTER_IMPLEMENTATION_PATHS = (
    "README.md",
    "auditctl/central.py",
    "auditctl/central_schema.py",
    "auditctl/vuoro_adapter.py",
    "docs/contracts/central-observation-ingest.md",
)


def test_dual_write_context_records_convergence_not_atomicity():
    packet = json.loads(
        (ROOT / "verification/contexts/sqlite-ndjson-convergence.json").read_text(encoding="utf-8")
    )

    assert packet["schema_version"] == "test-context/v1"
    assert packet["consistency"]["target"] == "projection-convergence"
    assert "crash-after-ndjson-fsync-before-sqlite-commit" in packet["faults"]
    assert packet["bounds"]["max_canonical_ndjson_line_bytes"] == 16384
    assert "oversized-event-is-rejected-before-a-shard-append-or-sqlite-commit" in packet["invariants"]
    assert "origin-sequences-are-unique-and-gap-free-for-valid-local-records" in packet["invariants"]
    assert "restart-add" in {operation["name"] for operation in packet["operations"]}


def test_protocol_document_rejects_cross_store_atomicity_claim():
    protocol = (ROOT / "docs/protocols/audit-write-and-rebuild.md").read_text(encoding="utf-8")

    assert "Cross-store atomicity is not claimed" in protocol
    assert "A successful response promises both copies" in protocol
    assert "This prevents a restart from assigning" in protocol
    assert "tuple to a different event" in protocol


def test_central_ingest_context_keeps_observation_and_authority_separate():
    packet = json.loads(
        (ROOT / "verification/contexts/central-observation-ingest.json").read_text(
            encoding="utf-8"
        )
    )

    assert packet["depth"] == 2
    assert "observations-create-no-authority-state" in packet["invariants"]
    assert "duplicate-upload" in packet["faults"]
    assert packet["bounds"]["read_limit"] == 100


def test_central_result_digest_matches_the_packaged_implementation_tree():
    """Some result on record must have been observed against the packaged tree.

    This asserted a single named packet until 2026-08-30, and that made every
    version bump falsify it: `pyproject.toml` is inside the digested set, so
    changing a version literal changes the digest of an implementation that did
    not change. The only route back to green was to rewrite the packet's
    `implementation_sha` -- and that packet is an observation, carrying its own
    claims, counterexamples and notes about the run that produced them. Restamping
    it makes that narrative describe a tree it was never run against, which is the
    one thing a ledger must not do.

    So the guarantee is kept and the singularity is dropped: the shipped tree must
    have been verified, and earlier observations stay true about the trees they
    were taken against.
    """
    digest = hashlib.sha256()
    assert CENTRAL_IMPLEMENTATION_PATHS == tuple(sorted(CENTRAL_IMPLEMENTATION_PATHS))
    for relative_path in CENTRAL_IMPLEMENTATION_PATHS:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    expected = f"sha256:central-implementation-v1:{digest.hexdigest()}"

    packets = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "verification/results").glob("*.json"))
    }
    central = {
        name: packet
        for name, packet in packets.items()
        if packet.get("context_id") == CENTRAL_CONTEXT_ID
    }
    assert central, f"no verification result declares context {CENTRAL_CONTEXT_ID}"

    matching = [name for name, packet in central.items() if packet["implementation_sha"] == expected]
    assert matching, (
        f"no result for {CENTRAL_CONTEXT_ID} was observed against the packaged tree.\n"
        f"  packaged: {expected}\n"
        "  on record: "
        + "\n             ".join(
            f"{name}: {packet['implementation_sha']}" for name, packet in sorted(central.items())
        )
        + "\nRe-run the central verification (docs/operations/running-the-central-verification.md) "
        "and add a result naming the work item that commissioned it. Do not edit an existing one."
    )


def test_vuoro_adapter_context_and_result_are_bound_to_the_owned_contract():
    context = json.loads(
        (ROOT / "verification/contexts/vuoro-audit-adapter.json").read_text(
            encoding="utf-8"
        )
    )
    result = json.loads(
        (
            ROOT / "verification/results/vuoro-audit-adapter-item-1202.json"
        ).read_text(encoding="utf-8")
    )
    contract_digest = hashlib.sha256(
        (ROOT / "docs/contracts/central-observation-ingest.md").read_bytes()
    ).hexdigest()

    assert context["depth"] == 2
    assert "lost-submit-response" in context["faults"]
    assert context["bounds"]["catalog_operations"] == 5
    assert result["context_id"] == context["id"]
    assert context["contract_ref"] == result["contract_ref"]
    assert context["contract_ref"]["revision"] == f"sha256:{contract_digest}"


def test_vuoro_adapter_result_digest_matches_the_owned_implementation_tree():
    packet = json.loads(
        (
            ROOT / "verification/results/vuoro-audit-adapter-item-1202.json"
        ).read_text(encoding="utf-8")
    )
    digest = hashlib.sha256()
    assert VUORO_ADAPTER_IMPLEMENTATION_PATHS == tuple(
        sorted(VUORO_ADAPTER_IMPLEMENTATION_PATHS)
    )
    for relative_path in VUORO_ADAPTER_IMPLEMENTATION_PATHS:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")

    assert packet["implementation_sha"] == (
        f"sha256:vuoro-audit-adapter-v1:{digest.hexdigest()}"
    )
