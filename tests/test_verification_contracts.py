import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    packet = json.loads(
        (
            ROOT / "verification/results/central-observation-ingest-item-1201.json"
        ).read_text(encoding="utf-8")
    )
    digest = hashlib.sha256()
    assert CENTRAL_IMPLEMENTATION_PATHS == tuple(sorted(CENTRAL_IMPLEMENTATION_PATHS))
    for relative_path in CENTRAL_IMPLEMENTATION_PATHS:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")

    assert packet["implementation_sha"] == (
        f"sha256:central-implementation-v1:{digest.hexdigest()}"
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
