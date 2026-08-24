import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
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
    assert "dispatch-result/v1" in normalized
    assert "dispatch_result_ref" in contract and "dispatch_result_digest" in contract
    assert "does not dereference the result" in normalized
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
                    "phase": "terminal",
                    "terminal_status": "completed",
                    "terminal_reason": "completed",
                    "dispatch_result_ref": "artifact:sha256:" + "a" * 64,
                    "dispatch_result_digest": "sha256:" + "a" * 64,
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
    assert event["metadata"]["phase"] == "terminal"
    assert event["metadata"]["terminal_status"] == "completed"
    assert event["metadata"]["terminal_reason"] == "completed"
    assert event["metadata"]["dispatch_result_ref"] == "artifact:sha256:" + "a" * 64
    assert event["metadata"]["dispatch_result_digest"] == "sha256:" + "a" * 64


def test_actionq_session_exit_result_metadata_survives_rebuild_round_trip(
    repo_root: Path, tmp_path: Path
) -> None:
    metadata = {
        "action_id": 2124,
        "session_id": "session-round-trip",
        "runtime_session_id": "session-round-trip",
        "phase": "finalizing",
        "terminal_status": "failed",
        "terminal_reason": "process-exit",
        "dispatch_result_ref": "artifact:sha256:" + "b" * 64,
        "dispatch_result_digest": "sha256:" + "b" * 64,
    }
    added = CliRunner().invoke(
        cli,
        [
            "add",
            "--type", "session.exit",
            "--source", "actionq-daemon",
            "--actor", "actionq:session-round-trip",
            "--summary", "Session exit observed",
            "--metadata", json.dumps(metadata, separators=(",", ":")),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert added.exit_code == 0, added.output

    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"
    (repo_root / ".auditctl" / "auditctl.db").unlink()
    rebuilt = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--replace"])
    assert rebuilt.exit_code == 0, rebuilt.output

    listed = CliRunner().invoke(cli, ["list", "--json", "--limit", "10"])
    assert listed.exit_code == 0, listed.output
    events = json.loads(listed.output)
    assert len(events) == 1
    assert events[0]["metadata"] == metadata


def _active_actionq_result_metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "action_id": 2124,
        "session_id": "session-contract",
        "runtime_session_id": "session-contract",
        "phase": "terminal",
        "terminal_status": "completed",
        "terminal_reason": "completed",
        "dispatch_result_ref": "artifact:sha256:" + "e" * 64,
        "dispatch_result_digest": "sha256:" + "e" * 64,
    }
    metadata.update(overrides)
    return metadata


@pytest.mark.parametrize(
    "unsafe_reason",
    [
        "token=secret-value",
        "/srv/actionq/worktrees/session-1/result.json",
        "worker failed after retrying the finalizer against the remote host",
    ],
)
def test_actionq_session_exit_rejects_unsafe_reason_on_add(
    repo_root: Path, tmp_path: Path, unsafe_reason: str
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type", "session.exit",
            "--source", "actionq-daemon",
            "--actor", "actionq:session-contract",
            "--summary", "Session exit observed",
            "--metadata", json.dumps(
                _active_actionq_result_metadata(terminal_reason=unsafe_reason),
                separators=(",", ":"),
            ),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert result.exit_code != 0
    assert "recognized safe reason code" in result.output
    assert not (tmp_path / "_artifacts" / "example-repo" / "audit").exists()


def test_actionq_session_exit_rejects_result_identity_binding_on_add(
    repo_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    missing_action = _active_actionq_result_metadata()
    del missing_action["action_id"]
    missing = runner.invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:session-contract", "--summary", "Session exit",
            "--metadata", json.dumps(missing_action, separators=(",", ":")),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert missing.exit_code != 0
    assert "missing: action_id" in missing.output

    mismatch = runner.invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:session-contract", "--summary", "Session exit",
            "--metadata", json.dumps(
                _active_actionq_result_metadata(runtime_session_id="other-session"),
                separators=(",", ":"),
            ),
            "--ts", "2026-04-26T10:00:01Z",
        ],
    )
    assert mismatch.exit_code != 0
    assert "session_id and runtime_session_id must match" in mismatch.output

    actor_mismatch = runner.invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:other-session", "--summary", "Session exit",
            "--metadata", json.dumps(_active_actionq_result_metadata(), separators=(",", ":")),
            "--ts", "2026-04-26T10:00:02Z",
        ],
    )
    assert actor_mismatch.exit_code != 0
    assert "actor must equal" in actor_mismatch.output
    assert not (tmp_path / "_artifacts" / "example-repo" / "audit").exists()


def test_actionq_session_exit_omitted_result_preserves_legacy_metadata(
    repo_root: Path, tmp_path: Path
) -> None:
    metadata = {
        "action_id": 21,
        "session_id": "legacy-session",
        "runtime_session_id": "legacy-session",
        "phase": None,
        "terminal_reason": {"path": "/tmp/legacy.log", "note": "old publisher"},
        "dispatch_result_ref": None,
        "dispatch_result_digest": None,
        "legacy_arbitrary": {"nested": [None, "unchanged"]},
    }
    result = CliRunner().invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:legacy-session", "--summary", "Legacy session exit",
            "--metadata", json.dumps(metadata, separators=(",", ":")),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert result.exit_code == 0, result.output
    event = json.loads(
        (tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson")
        .read_text(encoding="utf-8")
    )
    assert event["metadata"] == metadata


def test_actionq_session_exit_rebuild_rejects_unsafe_reason_before_mutation(
    repo_root: Path, tmp_path: Path
) -> None:
    added = CliRunner().invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:session-contract", "--summary", "Session exit",
            "--metadata", json.dumps(_active_actionq_result_metadata(), separators=(",", ":")),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert added.exit_code == 0, added.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    event["metadata"]["terminal_reason"] = "/srv/private/worker-output.txt"
    shard.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")
    before_db = (repo_root / ".auditctl" / "auditctl.db").read_bytes()
    before_shard = shard.read_bytes()

    rebuilt = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard), "--replace"])
    assert rebuilt.exit_code != 0
    assert "rebuild rejected [malformed_envelope]" in rebuilt.output
    assert (repo_root / ".auditctl" / "auditctl.db").read_bytes() == before_db
    assert shard.read_bytes() == before_shard


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "runtime_session_id",
            "other-session",
            "metadata.runtime_session_id must match the observation-envelope runtime_session_id",
        ),
        (
            "actor",
            "actionq:other-session",
            "actor must equal",
        ),
    ],
)
def test_actionq_session_exit_rebuild_rejects_envelope_identity_mismatch(
    repo_root: Path,
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    added = CliRunner().invoke(
        cli,
        [
            "add", "--type", "session.exit", "--source", "actionq-daemon",
            "--actor", "actionq:session-contract", "--summary", "Session exit",
            "--metadata", json.dumps(_active_actionq_result_metadata(), separators=(",", ":")),
            "--ts", "2026-04-26T10:00:00Z",
        ],
    )
    assert added.exit_code == 0, added.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-04-26.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    event[field] = value
    with pytest.raises(ValueError, match=error):
        validate_event_object(event)
    shard.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")
    before_db = (repo_root / ".auditctl" / "auditctl.db").read_bytes()
    before_shard = shard.read_bytes()

    rebuilt = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard), "--replace"])
    assert rebuilt.exit_code != 0
    assert "rebuild rejected [malformed_envelope]" in rebuilt.output
    assert (repo_root / ".auditctl" / "auditctl.db").read_bytes() == before_db
    assert shard.read_bytes() == before_shard


def test_legacy_session_exit_shard_rebuilds_with_opaque_metadata(
    repo_root: Path, tmp_path: Path
) -> None:
    metadata = {
        "phase": None,
        "terminal_reason": {"path": "/tmp/legacy.log", "text": "free-form legacy value"},
        "legacy_key": ["arbitrary", None],
    }
    shard = tmp_path / "legacy-session-exit.ndjson"
    shard.write_text(
        json.dumps(
            {
                "id": "ad:01HWXYZ0000000000000000000",
                "ts": "2026-04-26T10:00:00Z",
                "type": "session.exit",
                "actor": "actionq:legacy",
                "summary": "Legacy session exit",
                "detail": None,
                "refs": [],
                "source": "actionq-daemon",
                "metadata": metadata,
                "created_at": "2026-04-26T10:00:01Z",
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    rebuilt = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard), "--replace"])
    assert rebuilt.exit_code == 0, rebuilt.output
    listed = CliRunner().invoke(cli, ["list", "--json", "--limit", "10"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["metadata"] == metadata


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

    # candidate.reviewed was registered by contract version 2; later additive
    # versions bump the front matter, so pin the versioning note that records
    # which version introduced this mapping rather than the current version.
    assert "Version 2 adds `candidate.reviewed`" in normalized
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


def test_harness_baseline_contract_freezes_mapping_and_window_semantics() -> None:
    contract = (ROOT / "docs/contracts/publisher-subprocess.md").read_text(encoding="utf-8")
    normalized = " ".join(contract.split())

    assert "`harness.baseline`" in contract
    assert "harness-baseline" in contract
    assert "baseline:<baseline_hash>" in contract
    assert "contract_version: 3" in contract
    assert "Version 3 adds `harness.baseline`" in normalized
    # The window semantics are the load-bearing part of this mapping: silence
    # asserts the previous baseline, so both trust properties must stay frozen.
    assert "silence is the in-window state, not an absence of observation" in normalized
    assert "this publisher has no committing authority behind it" in normalized
    assert "A failed probe records its absence; it is never skipped." in normalized
    assert "report a stability it never observed" in normalized
    assert "Per-component digests are carried alongside the composite hash." in normalized
    assert "drift is attributable to the component that actually changed" in normalized
    for field in (
        "`baseline_hash`",
        "`component_digests`",
        "`changed_components`",
        "`collector=harness-baseline`",
        "`event_type=harness-baseline`",
    ):
        assert field in contract


def test_harness_baseline_first_observation_argv_emits_a_valid_shard_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    baseline_hash = "b" * 64
    component_digests = {
        "cli_version": "1" * 64,
        "auto_mode_rules": "2" * 64,
        "env_overrides": "3" * 64,
    }
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "harness.baseline",
            "--source",
            "harness-baseline",
            "--actor",
            "bayleaf",
            "--summary",
            f"Harness baseline established at {baseline_hash[:12]}",
            "--ref",
            f"baseline:{baseline_hash}",
            "--metadata",
            json.dumps(
                {
                    "event_type": "harness-baseline",
                    "baseline_hash": baseline_hash,
                    "component_digests": component_digests,
                    "changed_components": [],
                    "collector": "harness-baseline",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "--ts",
            "2026-08-24T10:00:00Z",
        ],
    )

    assert result.exit_code == 0, result.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-08-24.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert event["type"] == "harness.baseline"
    assert event["source"] == "harness-baseline"
    assert event["refs"] == [f"baseline:{baseline_hash}"]
    assert event["metadata"]["baseline_hash"] == baseline_hash
    assert event["metadata"]["changed_components"] == []
    assert event["metadata"]["component_digests"] == component_digests
    assert event["metadata"]["collector"] == "harness-baseline"
    assert event["record_class"] == "observation"
    # The ref carries the same composite hash the metadata reports, so repeat
    # observations of one baseline are reconcilable by ref alone.
    assert event["refs"][0].removeprefix("baseline:") == event["metadata"]["baseline_hash"]


def test_harness_baseline_moved_observation_names_changed_components(
    repo_root: Path, tmp_path: Path
) -> None:
    baseline_hash = "c" * 64
    changed = ["cli_version", "settings:/home/bayleaf/.claude/settings.json"]
    argv = [
        "add",
        "--type",
        "harness.baseline",
        "--source",
        "harness-baseline",
        "--actor",
        "bayleaf",
        "--summary",
        f"Harness baseline moved to {baseline_hash[:12]} ({len(changed)} component(s) changed)",
        "--ref",
        f"baseline:{baseline_hash}",
        "--metadata",
        json.dumps(
            {
                "event_type": "harness-baseline",
                "baseline_hash": baseline_hash,
                "component_digests": {
                    "cli_version": "4" * 64,
                    "settings:/home/bayleaf/.claude/settings.json": "5" * 64,
                },
                "changed_components": changed,
                "collector": "harness-baseline",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--ts",
        "2026-08-24T11:00:00Z",
    ]

    result = _invoke_auditctl_subprocess(repo_root, tmp_path, argv)
    assert result.returncode == 0, result.stderr

    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-08-24.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert event["metadata"]["changed_components"] == changed
    assert "component(s) changed" in event["summary"]
    # Only digests cross the boundary; raw component values never do.
    assert all(
        len(digest) == 64 for digest in event["metadata"]["component_digests"].values()
    )


def test_harness_baseline_absent_component_is_hashed_not_dropped(
    repo_root: Path, tmp_path: Path
) -> None:
    """A disappeared probe must move the hash and be named as a changed component.

    If a failed probe were dropped instead, the composite would be unchanged and
    the collector would assert a stability it never observed.
    """
    baseline_hash = "d" * 64
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "harness.baseline",
            "--source",
            "harness-baseline",
            "--actor",
            "bayleaf",
            "--summary",
            f"Harness baseline moved to {baseline_hash[:12]} (1 component(s) changed)",
            "--ref",
            f"baseline:{baseline_hash}",
            "--metadata",
            json.dumps(
                {
                    "event_type": "harness-baseline",
                    "baseline_hash": baseline_hash,
                    # The absent probe still has a digest of its absent-with-reason
                    # value, so it remains present in the component mapping.
                    "component_digests": {"cli_version": "6" * 64, "plugins": "7" * 64},
                    "changed_components": ["plugins"],
                    "collector": "harness-baseline",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "--ts",
            "2026-08-24T12:00:00Z",
        ],
    )

    assert result.exit_code == 0, result.output
    shard = tmp_path / "_artifacts" / "example-repo" / "audit" / "events-2026-08-24.ndjson"
    event = json.loads(shard.read_text(encoding="utf-8"))
    assert validate_event_object(event) == event
    assert "plugins" in event["metadata"]["component_digests"]
    assert event["metadata"]["changed_components"] == ["plugins"]


def test_harness_baseline_events_survive_rebuild_round_trip(
    repo_root: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    first_hash = "e" * 64
    second_hash = "f" * 64

    for baseline_hash, changed, summary in (
        (first_hash, [], f"Harness baseline established at {first_hash[:12]}"),
        (
            second_hash,
            ["cli_version"],
            f"Harness baseline moved to {second_hash[:12]} (1 component(s) changed)",
        ),
    ):
        added = runner.invoke(
            cli,
            [
                "add",
                "--type",
                "harness.baseline",
                "--source",
                "harness-baseline",
                "--actor",
                "bayleaf",
                "--summary",
                summary,
                "--ref",
                f"baseline:{baseline_hash}",
                "--metadata",
                json.dumps(
                    {
                        "event_type": "harness-baseline",
                        "baseline_hash": baseline_hash,
                        "component_digests": {"cli_version": "8" * 64},
                        "changed_components": changed,
                        "collector": "harness-baseline",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "--ts",
                "2026-08-24T13:00:00Z",
            ],
        )
        assert added.exit_code == 0, added.output

    shard_dir = tmp_path / "_artifacts" / "example-repo" / "audit"
    (repo_root / ".auditctl" / "auditctl.db").unlink()
    rebuild = runner.invoke(cli, ["rebuild", "--from-ndjson", str(shard_dir), "--replace"])
    assert rebuild.exit_code == 0, rebuild.output
    assert "2 imported" in rebuild.output

    listed = runner.invoke(cli, ["list", "--json", "--limit", "10"])
    assert listed.exit_code == 0, listed.output
    rebuilt_events = json.loads(listed.output)
    assert {event["type"] for event in rebuilt_events} == {"harness.baseline"}
    assert {event["refs"][0] for event in rebuilt_events} == {
        f"baseline:{first_hash}",
        f"baseline:{second_hash}",
    }
    by_hash = {e["metadata"]["baseline_hash"]: e for e in rebuilt_events}
    assert by_hash[first_hash]["metadata"]["changed_components"] == []
    assert by_hash[second_hash]["metadata"]["changed_components"] == ["cli_version"]
