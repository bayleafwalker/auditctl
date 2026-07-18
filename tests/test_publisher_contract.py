import json
from pathlib import Path

from click.testing import CliRunner

from auditctl.cli import cli
from auditctl.validation import validate_event_object


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


def test_sprintctl_close_argv_emits_a_valid_shard_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "sprint.closed",
            "--source",
            "sprintctl",
            "--actor",
            "publisher-user",
            "--summary",
            "Sprint 42 closed",
            "--ref",
            "sprint:42",
            "--metadata",
            json.dumps(
                {
                    "sprint_id": 42,
                    "event_type": "sprint-closed",
                    "boundary_revision": "event:9",
                },
                separators=(",", ":"),
            ),
            "--ts",
            "2026-04-26T10:00:00Z",
        ],
    )

    assert result.exit_code == 0, result.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert event["type"] == "sprint.closed"
    assert event["source"] == "sprintctl"
    assert event["refs"] == ["sprint:42"]
    assert event["metadata"]["boundary_revision"] == "event:9"
    assert event["record_class"] == "observation"
