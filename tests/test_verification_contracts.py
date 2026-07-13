import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dual_write_context_records_convergence_not_atomicity():
    packet = json.loads(
        (ROOT / "verification/contexts/sqlite-ndjson-convergence.json").read_text(encoding="utf-8")
    )

    assert packet["schema_version"] == "test-context/v1"
    assert packet["consistency"]["target"] == "projection-convergence"
    assert "crash-after-ndjson-fsync-before-sqlite-commit" in packet["faults"]


def test_protocol_document_rejects_cross_store_atomicity_claim():
    protocol = (ROOT / "docs/protocols/audit-write-and-rebuild.md").read_text(encoding="utf-8")

    assert "Cross-store atomicity is not claimed" in protocol
    assert "A successful response promises both copies" in protocol
