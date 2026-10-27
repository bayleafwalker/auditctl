from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import auditctl.cli as cli_module
from auditctl.cli import cli
from auditctl.semantic import build_event, interpretation_key, validate_semantic_event


def _legacy_event() -> dict:
    return {
        "id": "ad:00000000000000000000000001",
        "ts": "2026-10-25T12:00:00Z",
        "type": "ci.test-passed",
        "actor": "ci:worker-1",
        "summary": "unit checks passed",
        "detail": None,
        "refs": ["sha:" + "a" * 40],
        "source": "ci",
        "metadata": {"suite": "unit"},
        "created_at": "2026-10-25T12:00:01Z",
    }


def _manifest(
    tmp_path: Path, source_path: Path | None = None, *, source_id: str = "audit-ledger-1"
) -> tuple[Path, str, bytes]:
    raw_source = json.dumps(_legacy_event(), ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    source_path = source_path or (tmp_path / "frozen-events.ndjson")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(raw_source)
    manifest = {
        "schema_version": "auditctl-source-snapshot-manifest/v1",
        "sources": [
            {
                "source_id": source_id,
                "source_repo_id": "example-repo",
                "scope": "committed-shard",
                "path": str(source_path),
                "byte_count": len(raw_source),
                "line_count": 1,
                "sha256": hashlib.sha256(raw_source).hexdigest(),
                "record_digest_domain": "unknown",
            }
        ],
    }
    raw_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / f"{source_id}.manifest.json"
    manifest_path.write_bytes(raw_manifest)
    return manifest_path, hashlib.sha256(raw_manifest).hexdigest(), raw_source


def _result(manifest_sha256: str) -> dict:
    value = {
        "schema_version": "semantic-observation/v1",
        "case_id": "case-42",
        "source_basis_ref": f"artifact:sha256:{manifest_sha256}",
        "source_basis_sha256": f"sha256:{manifest_sha256}",
        "interpretation_key": "sha256:" + "0" * 64,
        "work_ref": "wi:1842",
        "observed_work_revision": 12,
        "statement": "unit evidence is complete",
        "kind": "model-inferred",
        "producer_model": "provider/model@revision-7",
        "profile": "bounded-review-v1",
        "prompt_or_recipe_digest": "sha256:" + "1" * 64,
        "extracted_at": "2026-10-26T05:00:00Z",
        "analysis_generation": 1,
        "supersedes": None,
        "applicability": "exact-basis-and-work-revision",
        "usage": {
            "status": "reported",
            "input_tokens": 20,
            "output_tokens": 4,
            "cost_microusd": 11,
            "billing_source": "provider-billed",
        },
    }
    value["interpretation_key"] = interpretation_key(value)
    return value


def _write_result(
    tmp_path: Path, result: dict, *, path: Path | None = None
) -> tuple[Path, str]:
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    path = path or (tmp_path / "semantic-result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _args(manifest: Path, manifest_sha: str, result: Path, result_sha: str) -> list[str]:
    return [
        "semantic-replay",
        "--manifest",
        str(manifest),
        "--manifest-sha256",
        manifest_sha,
        "--result",
        str(result),
        "--result-sha256",
        result_sha,
    ]


def _invoke(manifest: Path, manifest_sha: str, result: Path, result_sha: str):
    return CliRunner().invoke(cli, _args(manifest, manifest_sha, result, result_sha))


def _concurrent_replay_worker(barrier, result_queue, args: list[str]) -> None:
    original = cli_module._record_event

    def synchronized_record(*call_args, **call_kwargs):
        barrier.wait(timeout=15)
        return original(*call_args, **call_kwargs)

    cli_module._record_event = synchronized_record
    result = CliRunner().invoke(cli, args)
    result_queue.put((result.exit_code, result.output))


def test_semantic_replay_records_a_source_bound_observation_and_keeps_basis_bytes(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, original_source = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))
    source = tmp_path / "frozen-events.ndjson"

    output = _invoke(manifest, manifest_sha, result, result_sha)

    assert output.exit_code == 0, output.output
    receipt = json.loads(output.output)
    assert receipt["duplicate"] is False
    assert receipt["basis_qualification"] == "source-only-qualified"
    assert receipt["source_basis_ref"] == f"artifact:sha256:{manifest_sha}"
    assert source.read_bytes() == original_source

    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert listed.exit_code == 0, listed.output
    [event] = json.loads(listed.output)
    assert event["record_class"] == "observation"
    assert event["actor"] == "auditctl-semantic-consumer"
    assert event["source"] == "semantic-consumer"
    assert event["refs"] == ["wi:1842"]
    assert event["metadata"]["semantic_observation"]["source_basis_sha256"] == f"sha256:{manifest_sha}"
    assert event["metadata"]["semantic_result_sha256"] == f"sha256:{result_sha}"
    assert event["metadata"]["correlation_id"] == "case-42"
    assert event["basis_revision"] == "12"


def test_semantic_event_rechecks_the_persisted_exact_result_digest() -> None:
    value = _result("a" * 64)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    event = build_event(value, hashlib.sha256(raw).hexdigest())
    validate_semantic_event(event)

    event["metadata"]["semantic_result_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="semantic_result_sha256 differs from the canonical result bytes"):
        validate_semantic_event(event)


def test_generic_add_cannot_bypass_semantic_replay_deduplication(
    repo_root: Path, tmp_path: Path
) -> None:
    _, manifest_sha, _ = _manifest(tmp_path)
    value = _result(manifest_sha)
    output = CliRunner().invoke(
        cli,
        [
            "add",
            "--type",
            "semantic.observation",
            "--source",
            "semantic-consumer",
            "--actor",
            "auditctl-semantic-consumer",
            "--summary",
            value["statement"],
            "--ref",
            value["work_ref"],
            "--metadata",
            json.dumps(
                {
                    "semantic_observation": value,
                    "basis_revision": str(value["observed_work_revision"]),
                    "correlation_id": value["case_id"],
                }
            ),
            "--ts",
            value["extracted_at"],
        ],
    )

    assert output.exit_code != 0
    assert "semantic.observation must use semantic-replay" in output.output
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()


def test_semantic_replay_is_idempotent_for_same_key_and_payload(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))

    first = _invoke(manifest, manifest_sha, result, result_sha)
    second = _invoke(manifest, manifest_sha, result, result_sha)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_receipt = json.loads(first.output)
    second_receipt = json.loads(second.output)
    assert first_receipt["event_id"] == second_receipt["event_id"]
    assert first_receipt["origin_seq"] == second_receipt["origin_seq"] == 1
    assert second_receipt["duplicate"] is True
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert len(json.loads(listed.output)) == 1


def test_semantic_replay_new_generation_links_a_prior_observation(
    repo_root: Path, tmp_path: Path
) -> None:
    first_manifest, first_manifest_sha, _ = _manifest(tmp_path)
    first_result, first_result_sha = _write_result(
        tmp_path, _result(first_manifest_sha)
    )
    first = _invoke(first_manifest, first_manifest_sha, first_result, first_result_sha)
    assert first.exit_code == 0, first.output
    first_receipt = json.loads(first.output)

    next_manifest, next_manifest_sha, _ = _manifest(tmp_path, source_id="audit-ledger-2")
    next_value = _result(next_manifest_sha)
    next_value["analysis_generation"] = 2
    next_value["supersedes"] = first_receipt["event_id"]
    next_value["interpretation_key"] = interpretation_key(next_value)
    next_result, next_result_sha = _write_result(tmp_path, next_value)

    next_replay = _invoke(next_manifest, next_manifest_sha, next_result, next_result_sha)

    assert next_replay.exit_code == 0, next_replay.output
    next_receipt = json.loads(next_replay.output)
    assert next_receipt["event_id"] != first_receipt["event_id"]
    assert next_receipt["origin_seq"] == 2
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert listed.exit_code == 0, listed.output
    observations = json.loads(listed.output)
    assert len(observations) == 2
    followup = next(
        item for item in observations
        if item["metadata"]["semantic_observation"]["analysis_generation"] == 2
    )
    assert followup["metadata"]["semantic_observation"]["supersedes"] == first_receipt["event_id"]


def test_semantic_replay_requires_and_checks_generation_predecessor(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    missing_link = _result(manifest_sha)
    missing_link["analysis_generation"] = 2
    missing_link["interpretation_key"] = interpretation_key(missing_link)
    result, result_sha = _write_result(tmp_path, missing_link)

    missing = _invoke(manifest, manifest_sha, result, result_sha)

    assert missing.exit_code != 0
    assert "later generations require a prior event reference" in missing.output
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()

    missing_link["supersedes"] = "ad:00000000000000000000000001"
    missing_link["interpretation_key"] = interpretation_key(missing_link)
    result, result_sha = _write_result(tmp_path, missing_link)
    unknown_prior = _invoke(manifest, manifest_sha, result, result_sha)

    assert unknown_prior.exit_code != 0
    assert "supersedes_conflict" in unknown_prior.output
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == []


def test_semantic_replay_serializes_two_independent_processes_for_one_key(
    repo_root: Path, tmp_path: Path
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("process barrier test requires the POSIX fork start method")
    manifest, manifest_sha, _ = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))
    args = _args(manifest, manifest_sha, result, result_sha)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    workers = [
        context.Process(target=_concurrent_replay_worker, args=(barrier, result_queue, args))
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    for worker in workers:
        assert not worker.is_alive(), "semantic replay process did not finish"
        assert worker.exitcode == 0

    outcomes = [result_queue.get(timeout=5) for _ in workers]
    assert all(code == 0 for code, _ in outcomes), outcomes
    receipts = [json.loads(output) for _, output in outcomes]
    assert receipts[0]["event_id"] == receipts[1]["event_id"]
    assert receipts[0]["origin_seq"] == receipts[1]["origin_seq"] == 1
    assert sorted(receipt["duplicate"] for receipt in receipts) == [False, True]
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert listed.exit_code == 0, listed.output
    assert len(json.loads(listed.output)) == 1


def test_semantic_replay_rejects_same_key_with_changed_content(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    original = _result(manifest_sha)
    result, result_sha = _write_result(tmp_path, original)
    first = _invoke(manifest, manifest_sha, result, result_sha)
    changed = dict(original, statement="different model output")
    changed_path, changed_sha = _write_result(tmp_path, changed)

    conflict = _invoke(manifest, manifest_sha, changed_path, changed_sha)

    assert first.exit_code == 0, first.output
    assert conflict.exit_code != 0
    assert "interpretation_key_conflict" in conflict.output
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert len(json.loads(listed.output)) == 1


def test_semantic_replay_recovers_after_ndjson_append_with_lost_local_response(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))
    real_append = cli_module.append_event
    first_call = True

    def append_then_lose_response(path: Path, event: dict) -> None:
        nonlocal first_call
        real_append(path, event)
        if first_call:
            first_call = False
            raise OSError("simulated lost local response after append")

    monkeypatch.setattr(cli_module, "append_event", append_then_lose_response)
    failed = _invoke(manifest, manifest_sha, result, result_sha)
    recovered = _invoke(manifest, manifest_sha, result, result_sha)

    assert failed.exit_code != 0
    assert recovered.exit_code == 0, recovered.output
    receipt = json.loads(recovered.output)
    assert receipt["duplicate"] is True
    assert receipt["origin_seq"] == 1
    listed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert len(json.loads(listed.output)) == 1


def test_semantic_observation_rebuild_round_trips_result_and_digest(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))
    published = _invoke(manifest, manifest_sha, result, result_sha)
    assert published.exit_code == 0, published.output
    receipt = json.loads(published.output)
    shard = Path(receipt["ndjson_path"])

    database = repo_root / ".auditctl" / "auditctl.db"
    database.unlink()
    rebuilt = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard)])
    assert rebuilt.exit_code == 0, rebuilt.output
    replayed = CliRunner().invoke(cli, ["list", "--type", "semantic.observation", "--json"])
    assert replayed.exit_code == 0, replayed.output
    [event] = json.loads(replayed.output)
    assert event["event_id"] == receipt["event_id"]
    assert event["metadata"]["semantic_result_sha256"] == f"sha256:{result_sha}"
    assert event["metadata"]["semantic_observation"] == _result(manifest_sha)

    rebuilt_again = CliRunner().invoke(cli, ["rebuild", "--from-ndjson", str(shard)])
    assert rebuilt_again.exit_code == 0, rebuilt_again.output
    assert "0 imported, 1 skipped" in rebuilt_again.output


def test_semantic_replay_rejects_changed_source_and_result_basis_mismatch(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))
    source = tmp_path / "frozen-events.ndjson"
    source.write_bytes(b"changed bytes\n")

    changed_source = _invoke(manifest, manifest_sha, result, result_sha)

    assert changed_source.exit_code != 0
    assert "frozen source changed" in changed_source.output
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()

    new_manifest, new_manifest_sha, _ = _manifest(tmp_path, source_id="audit-ledger-2")
    basis_mismatch = _invoke(new_manifest, new_manifest_sha, result, result_sha)
    assert basis_mismatch.exit_code != 0
    assert "source_basis_ref must identify the verified manifest bytes" in basis_mismatch.output


def test_semantic_replay_rejects_result_hash_and_unknown_fields(
    repo_root: Path, tmp_path: Path
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    value = _result(manifest_sha)
    result, _ = _write_result(tmp_path, value)

    wrong_hash = _invoke(manifest, manifest_sha, result, "0" * 64)
    assert wrong_hash.exit_code != 0
    assert "result differs from its frozen SHA-256" in wrong_hash.output

    value["prompt"] = "raw prompt is not accepted"
    extra_path, extra_sha = _write_result(tmp_path, value)
    extra = _invoke(manifest, manifest_sha, extra_path, extra_sha)
    assert extra.exit_code != 0
    assert "invalid semantic observation fields" in extra.output

    noncanonical_path = tmp_path / "noncanonical-result.json"
    noncanonical_raw = json.dumps(_result(manifest_sha), indent=2).encode()
    noncanonical_path.write_bytes(noncanonical_raw)
    noncanonical = _invoke(
        manifest, manifest_sha, noncanonical_path, hashlib.sha256(noncanonical_raw).hexdigest()
    )
    assert noncanonical.exit_code != 0
    assert "canonical UTF-8 JSON bytes" in noncanonical.output


def test_semantic_replay_does_not_append_to_a_source_shard(
    repo_root: Path, tmp_path: Path
) -> None:
    source_path = repo_root / "_artifacts" / repo_root.name / "audit" / "events-2026-10-26.ndjson"
    manifest, manifest_sha, source_bytes = _manifest(tmp_path, source_path)
    result, result_sha = _write_result(tmp_path, _result(manifest_sha))

    output = _invoke(manifest, manifest_sha, result, result_sha)

    assert output.exit_code != 0
    assert "replay destination shard aliases an immutable replay input" in output.output
    assert source_path.read_bytes() == source_bytes
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()


@pytest.mark.parametrize("input_kind", ["manifest", "result"])
def test_semantic_replay_does_not_append_to_manifest_or_result_input(
    repo_root: Path, tmp_path: Path, input_kind: str
) -> None:
    manifest, manifest_sha, _ = _manifest(tmp_path)
    output_path = (
        repo_root / "_artifacts" / repo_root.name / "audit" / "events-2026-10-26.ndjson"
    )
    result_value = _result(manifest_sha)
    if input_kind == "manifest":
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(manifest.read_bytes())
        manifest_path = output_path
        result_path, result_sha = _write_result(tmp_path, result_value)
        preserved_input = output_path.read_bytes()
    else:
        manifest_path = manifest
        result_path, result_sha = _write_result(tmp_path, result_value, path=output_path)
        preserved_input = output_path.read_bytes()

    output = _invoke(manifest_path, manifest_sha, result_path, result_sha)

    assert output.exit_code != 0
    assert "replay destination shard aliases an immutable replay input" in output.output
    assert output_path.read_bytes() == preserved_input
    assert not (repo_root / ".auditctl" / "auditctl.db").exists()
