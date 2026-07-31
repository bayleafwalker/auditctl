import json
import os
from pathlib import Path
import subprocess
import sys

from click.testing import CliRunner

from auditctl.cli import cli
from auditctl.validation import validate_event_object


ROOT = Path(__file__).resolve().parents[1]


def _invoke_auditctl_subprocess(
    repo_root: Path, tmp_path: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AUDITCTL_DB"] = str(repo_root / ".auditctl" / "auditctl.db")
    env["AUDITCTL_ARTIFACTS_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), env.get("PYTHONPATH")) if part
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from auditctl.cli import cli; cli()",
            *args,
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


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


def test_session_mechanization_contract_freezes_event_set() -> None:
    contract = (ROOT / "docs/contracts/publisher-subprocess.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    for event_type in (
        "session.started",
        "session.ended",
        "session.end-inferred",
        "session.capsule-pointer",
    ):
        assert f"`{event_type}`" in contract
    assert "session-wrapper" in contract
    assert "does not claim the wrapper has shipped" in normalized
    assert "runtime_session_id" in contract and "capsule_id" in contract
    assert "capsule:<capsule_id>" in contract
    assert "non-validation-bearing" in normalized
    assert "does not interpret session liveness, store raw prompts/transcripts, or mutate sprint state" in normalized


def test_session_started_argv_emits_a_valid_shard_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    runtime_session_id = "runsess-0001"
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "session.started",
            "--source",
            "session-wrapper",
            "--actor",
            "session-wrapper:devbox",
            "--summary",
            "Session runsess-0001 started",
            "--metadata",
            json.dumps(
                {
                    "runtime_session_id": runtime_session_id,
                    "repo_project": "auditctl",
                    "harness": "claude-code",
                    "model": "claude-sonnet-5",
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
    assert event["type"] == "session.started"
    assert event["source"] == "session-wrapper"
    assert event["runtime_session_id"] == runtime_session_id
    assert event["metadata"]["harness"] == "claude-code"


def test_session_ended_and_end_inferred_argv_emit_valid_shard_observations(
    repo_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    runtime_session_id = "runsess-0002"
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"

    ended = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "session.ended",
            "--source",
            "session-wrapper",
            "--actor",
            "session-wrapper:devbox",
            "--summary",
            "Session runsess-0002 ended",
            "--metadata",
            json.dumps(
                {"runtime_session_id": runtime_session_id, "end_reason": "clean-exit"},
                separators=(",", ":"),
            ),
            "--ts",
            "2026-04-26T10:00:01Z",
        ],
    )
    assert ended.exit_code == 0, ended.output

    inferred = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "session.end-inferred",
            "--source",
            "session-wrapper",
            "--actor",
            "session-wrapper:devbox",
            "--summary",
            "Session runsess-0003 end inferred",
            "--metadata",
            json.dumps(
                {"runtime_session_id": "runsess-0003", "end_reason": "crash-recovery"},
                separators=(",", ":"),
            ),
            "--ts",
            "2026-04-26T10:00:02Z",
        ],
    )
    assert inferred.exit_code == 0, inferred.output

    events = [json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in events] == ["session.ended", "session.end-inferred"]
    for event in events:
        assert validate_event_object(event) == event
    assert events[0]["runtime_session_id"] == runtime_session_id
    assert events[1]["metadata"]["end_reason"] == "crash-recovery"


def test_session_capsule_pointer_argv_emits_a_valid_shard_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    runtime_session_id = "runsess-0004"
    capsule_id = "01f2b3c4-5555-4666-8777-999999999999"
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "session.capsule-pointer",
            "--source",
            "session-wrapper",
            "--actor",
            "session-wrapper:devbox",
            "--summary",
            f"Capsule {capsule_id} finalized",
            "--ref",
            f"capsule:{capsule_id}",
            "--metadata",
            json.dumps(
                {"runtime_session_id": runtime_session_id, "capsule_id": capsule_id},
                separators=(",", ":"),
            ),
            "--ts",
            "2026-04-26T10:00:03Z",
        ],
    )

    assert result.exit_code == 0, result.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert event["type"] == "session.capsule-pointer"
    assert event["refs"] == [f"capsule:{capsule_id}"]
    assert event["metadata"]["capsule_id"] == capsule_id


def test_session_mechanization_events_survive_rebuild_round_trip(
    repo_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    runtime_session_id = "runsess-0005"
    capsule_id = "11f2b3c4-5555-4666-8777-999999999999"
    common_metadata = {"runtime_session_id": runtime_session_id}

    for event_type, extra, ref in (
        ("session.started", {"repo_project": "auditctl", "harness": "claude-code"}, None),
        ("session.ended", {"end_reason": "clean-exit"}, None),
        ("session.capsule-pointer", {"capsule_id": capsule_id}, f"capsule:{capsule_id}"),
    ):
        args = [
            "add",
            "--type",
            event_type,
            "--source",
            "session-wrapper",
            "--actor",
            "session-wrapper:devbox",
            "--summary",
            f"{event_type} event",
            "--metadata",
            json.dumps({**common_metadata, **extra}, separators=(",", ":")),
            "--ts",
            "2026-04-26T10:00:00Z",
        ]
        if ref:
            args.extend(["--ref", ref])
        added = runner.invoke(cli, args)
        assert added.exit_code == 0, added.output

    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"
    (repo_root / ".auditctl" / "auditctl.db").unlink()
    rebuild = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--replace"])
    assert rebuild.exit_code == 0, rebuild.output
    assert "3 imported" in rebuild.output

    listed = runner.invoke(cli, ["list", "--json", "--limit", "10"])
    assert listed.exit_code == 0, listed.output
    rebuilt_events = json.loads(listed.output)
    assert {event["type"] for event in rebuilt_events} == {
        "session.started",
        "session.ended",
        "session.capsule-pointer",
    }
    assert all(event["runtime_session_id"] == runtime_session_id for event in rebuilt_events)


def test_candidate_reviewed_contract_freezes_exact_mapping_and_retry_posture() -> None:
    contract = (ROOT / "docs/contracts/publisher-subprocess.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    assert "contract_version: 2" in contract
    assert "`candidate.reviewed`" in contract
    assert "`source` is `actionq-review`" in normalized
    assert "authenticated review identity" in normalized
    assert "`sha:<reviewed-git-commit>`" in contract
    for field in (
        "action_id",
        "attempt_id",
        "plan_ref",
        "subject_kind",
        "publication_ref",
        "verification_result_ref",
        "review_result_artifact_ref",
        "topology",
        "findings_digest",
        "review_outcome",
        "runtime_session_id",
    ):
        assert f"`{field}`" in contract
    assert "either `candidate` or `integration`" in normalized
    assert "either `no-findings` or `findings-recorded`" in normalized
    assert "performs no blind retry" in normalized
    assert "retries only when that observation is absent" in normalized
    assert "does not make auditctl insertion exactly once" in normalized
    assert "no approval, acceptance, merge, or release field" in normalized


def test_candidate_reviewed_subprocess_emits_exact_redacted_mapping(
    repo_root: Path, tmp_path: Path
) -> None:
    metadata = {
        "action_id": "action-2035",
        "attempt_id": "attempt-7",
        "plan_ref": "artifact:sha256:" + "1" * 64,
        "subject_kind": "candidate",
        "publication_ref": "artifact:sha256:" + "2" * 64,
        "verification_result_ref": "artifact:sha256:" + "3" * 64,
        "review_result_artifact_ref": "artifact:sha256:" + "4" * 64,
        "topology": "independent-fresh-context",
        "findings_digest": "sha256:" + "5" * 64,
        "review_outcome": "findings-recorded",
        "runtime_session_id": "review-session-9",
    }
    reviewed_commit = "a" * 40
    result = _invoke_auditctl_subprocess(
        repo_root,
        tmp_path,
        [
            "add",
            "--type",
            "candidate.reviewed",
            "--source",
            "actionq-review",
            "--actor",
            "reviewer:oidc:subject-17",
            "--summary",
            "Independent candidate review recorded",
            "--ref",
            f"sha:{reviewed_commit}",
            "--ref",
            "wi:2035",
            "--ref",
            "sprint:582",
            "--metadata",
            json.dumps(metadata, separators=(",", ":")),
            "--ts",
            "2026-07-31T12:00:00Z",
        ],
    )

    assert result.returncode == 0, result.stderr
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-07-31.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert event["type"] == "candidate.reviewed"
    assert event["source"] == "actionq-review"
    assert event["actor"] == "reviewer:oidc:subject-17"
    assert event["refs"] == [f"sha:{reviewed_commit}", "wi:2035", "sprint:582"]
    assert event["metadata"] == metadata
    assert set(event["metadata"]) == set(metadata)
    assert event["runtime_session_id"] == "review-session-9"
    serialized = json.dumps(event, sort_keys=True)
    assert "approval" not in serialized.lower()
    assert "raw findings" not in serialized.lower()
    assert "credential" not in serialized.lower()


def test_candidate_reviewed_subprocess_blind_retry_demonstrates_reconcile_requirement(
    repo_root: Path, tmp_path: Path
) -> None:
    metadata = {
        "action_id": "action-2035",
        "attempt_id": "attempt-8",
        "plan_ref": "artifact:sha256:" + "6" * 64,
        "subject_kind": "integration",
        "publication_ref": "artifact:sha256:" + "7" * 64,
        "verification_result_ref": "artifact:sha256:" + "8" * 64,
        "review_result_artifact_ref": "artifact:sha256:" + "9" * 64,
        "topology": "integration-tip",
        "findings_digest": "sha256:" + "b" * 64,
        "review_outcome": "no-findings",
    }
    argv = [
        "add",
        "--type",
        "candidate.reviewed",
        "--source",
        "actionq-review",
        "--actor",
        "reviewer:oidc:subject-18",
        "--summary",
        "Independent integration review recorded",
        "--ref",
        "sha:" + "c" * 40,
        "--metadata",
        json.dumps(metadata, separators=(",", ":")),
        "--ts",
        "2026-07-31T12:01:00Z",
    ]

    first = _invoke_auditctl_subprocess(repo_root, tmp_path, argv)
    second = _invoke_auditctl_subprocess(repo_root, tmp_path, argv)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-07-31.ndjson"
    events = [json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 2
    assert events[0]["id"] != events[1]["id"]
    retry_key = (
        "source",
        "type",
        "action_id",
        "attempt_id",
        "plan_ref",
        "subject_kind",
        "publication_ref",
        "verification_result_ref",
        "review_result_artifact_ref",
    )
    projected = [
        (
            event["source"],
            event["type"],
            *(event["metadata"][field] for field in retry_key[2:]),
        )
        for event in events
    ]
    assert projected[0] == projected[1]
