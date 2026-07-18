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


def test_actionq_contract_freezes_event_set_without_claiming_caller_shipped() -> None:
    contract = (ROOT / "docs/contracts/publisher-subprocess.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    for event_type in (
        "dispatch.queued",
        "dispatch.started",
        "session.start",
        "session.pause",
        "session.resume",
        "session.exit",
        "pr.open",
        "pr.merge",
    ):
        assert f"`{event_type}`" in contract
    assert "does not claim the daemon or its caller has shipped" in normalized
    assert "Actionq #973 owns that caller" in normalized
    assert "session_id" in contract and "runtime_session_id" in contract
    assert "fail_action_on_emit_error=false" in normalized
    assert "performs no blind retry" in normalized
    assert "not an exactly-once guarantee" in normalized


def test_actionq_session_exit_argv_emits_a_valid_shard_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    session_id = "session-019f"
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "session.exit",
            "--source",
            "actionq-daemon",
            "--actor",
            f"actionq:{session_id}",
            "--summary",
            "Session session-019f exited completed",
            "--ref",
            "wi:1154",
            "--ref",
            "sprint:414",
            "--metadata",
            json.dumps(
                {
                    "action_id": 21,
                    "session_id": session_id,
                    "runtime_session_id": session_id,
                    "harness": "codex",
                    "model": "gpt-5",
                    "outcome": "completed",
                    "branch": "agent/scope-iterate/21",
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
    assert event["type"] == "session.exit"
    assert event["source"] == "actionq-daemon"
    assert event["refs"] == ["wi:1154", "sprint:414"]
    assert event["runtime_session_id"] == session_id
    assert event["metadata"]["action_id"] == 21
