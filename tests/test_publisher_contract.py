from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sprintctl_publisher_contract_closes_mapping_and_failure_posture() -> None:
    contract = (ROOT / "docs/contracts/publisher-subprocess.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    for event_type in (
        "sprint.opened",
        "sprint.closed",
        "sprint.taken_up",
        "sprint.released",
        "knowledge.landed",
    ):
        assert f"`{event_type}`" in contract
    assert "10-second timeout" in normalized
    assert "does not reverse or fail the already-committed sprintctl operation" in normalized
    assert "performs no automatic retry" in normalized
    assert "blind caller retry can create two valid observations" in normalized
    assert "do not import auditctl as a Python library" in normalized
