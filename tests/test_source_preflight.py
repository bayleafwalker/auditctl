from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from auditctl.central import prepare_observation
from auditctl.cli import cli
from auditctl.source_preflight import SourcePreflightError, preflight
from auditctl.validation import with_observation_envelope


STREAM = "40b89732-b2c7-4f60-98c2-199a960c2a20"


def _legacy(event_id: str, summary: str = "legacy") -> dict:
    return {
        "id": event_id,
        "ts": "2026-10-17T12:00:00Z",
        "type": "session.started",
        "actor": "codex:test",
        "summary": summary,
        "detail": None,
        "refs": [],
        "source": "fixture",
        "metadata": {},
        "created_at": "2026-10-17T12:00:01Z",
    }


def _enveloped(event_id: str, seq: int, record_class: str = "observation") -> dict:
    return with_observation_envelope(
        _legacy(event_id, "café 東京"), origin_stream_id=STREAM, origin_seq=seq,
        record_class=record_class,
    )


def _write_manifest(
    tmp_path: Path,
    sources: list[tuple[str, list[dict]]],
    *,
    record_digest_domains: dict[str, str] | None = None,
) -> tuple[Path, str]:
    descriptors = []
    for index, (source_id, events) in enumerate(sources):
        raw = b"".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            for event in events
        )
        path = tmp_path / f"private-fixture-{index}.ndjson"
        path.write_bytes(raw)
        descriptors.append({
            "source_id": source_id,
            "source_repo_id": "audit-fixture",
            "scope": "committed-shard",
            "path": path.name,
            "byte_count": len(raw),
            "line_count": len(events),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        if record_digest_domains and source_id in record_digest_domains:
            descriptors[-1]["record_digest_domain"] = record_digest_domains[source_id]
    manifest = {"schema_version": "auditctl-source-snapshot-manifest/v1", "sources": descriptors}
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / "frozen-manifest.json"
    manifest_path.write_bytes(manifest_raw)
    return manifest_path, hashlib.sha256(manifest_raw).hexdigest()


def test_preflight_preserves_unicode_legacy_and_observation_decision_digest_domains(tmp_path: Path) -> None:
    legacy = _legacy("ad:00000000000000000000000001", "legacy café")
    observation = _enveloped("ad:00000000000000000000000002", 1, "observation")
    decision = _enveloped("ad:00000000000000000000000003", 2, "decision")
    path, digest = _write_manifest(tmp_path, [("source-a", [legacy, observation, decision])])

    result = preflight(path, digest)

    assert result["qualification"] == "source-only-qualified"
    assert result["s4_authority_established"] is False
    assert result["cutover_authorized"] is False
    assert result["distinct_event_count"] == 3
    records = {record["event_id"]: record for record in result["records"] if "line" in record}
    assert records[legacy["id"]]["source_payload_sha256"] == {"status": "absent", "sha256": None}
    assert records[legacy["id"]]["source_record_sha256"]["status"] == "absent"
    assert records[legacy["id"]]["derived_auditctl_central_v1_record_sha256"] is None
    assert records[observation["id"]]["record_class"] == "observation"
    assert records[decision["id"]]["record_class"] == "decision"
    assert records[observation["id"]]["source_payload_sha256"]["status"] == "present"
    assert records[observation["id"]]["source_record_sha256"]["status"] == "absent"
    assert records[observation["id"]]["derived_auditctl_central_v1_record_sha256"]
    assert records[observation["id"]]["raw_line_sha256"]
    assert records[observation["id"]]["byte_offset"] > records[legacy["id"]]["byte_offset"]


def test_preflight_labels_only_the_owner_central_digest_domain(tmp_path: Path) -> None:
    event = _enveloped("ad:00000000000000000000000001", 1)
    event["record_sha256"] = prepare_observation(event).record_sha256
    legacy = _legacy("ad:00000000000000000000000002")
    legacy["record_sha256"] = "e" * 64
    path, digest = _write_manifest(
        tmp_path,
        [("source-a", [event, legacy])],
        record_digest_domains={"source-a": "auditctl-central-v1"},
    )

    result = preflight(path, digest)
    records = {record["event_id"]: record for record in result["records"]}
    assert records[event["id"]]["source_record_sha256"] == {
        "status": "verified", "domain": "auditctl-central-v1", "sha256": event["record_sha256"]
    }
    assert records[legacy["id"]]["source_record_sha256"] == {
        "status": "unverifiable-domain", "domain": "auditctl-central-v1", "sha256": "e" * 64
    }
    assert result["record_digest_qualification"] == "partial"
    assert result["unverifiable_record_digest_count"] == 1

    event["record_sha256"] = "f" * 64
    path, digest = _write_manifest(
        tmp_path,
        [("source-a", [event])],
        record_digest_domains={"source-a": "auditctl-central-v1"},
    )
    with pytest.raises(SourcePreflightError, match="differs from Auditctl central-v1 digest"):
        preflight(path, digest)

    producer_digest = hashlib.sha256(b"producer-specific-record-digest-domain").hexdigest()
    event["record_sha256"] = producer_digest
    legacy = _legacy("ad:00000000000000000000000002")
    legacy_digest = hashlib.sha256(b"legacy-record-digest-domain").hexdigest()
    legacy["record_sha256"] = legacy_digest
    path, digest = _write_manifest(tmp_path, [("source-a", [event, legacy])])
    result = preflight(path, digest)
    unknown_records = {record["event_id"]: record for record in result["records"]}
    assert unknown_records[event["id"]]["source_record_sha256"] == {
        "status": "unknown-domain", "domain": "unknown", "sha256": producer_digest
    }
    assert unknown_records[legacy["id"]]["source_record_sha256"] == {
        "status": "unknown-domain", "domain": "unknown", "sha256": legacy_digest
    }
    assert result["unknown_record_digest_count"] == 2


def test_preflight_counts_exact_copies_separately_and_rejects_changed_identity(tmp_path: Path) -> None:
    event = _legacy("ad:00000000000000000000000001")
    path, digest = _write_manifest(tmp_path, [("source-a", [event]), ("source-b", [event])])
    result = preflight(path, digest)
    assert result["distinct_event_count"] == 1
    assert result["exact_duplicate_copies"] == 1

    conflicting = _legacy(event["id"], "changed bytes")
    path, digest = _write_manifest(tmp_path, [("source-a", [event]), ("source-b", [conflicting])])
    with pytest.raises(SourcePreflightError, match="conflicting stable event identity"):
        preflight(path, digest)


def test_preflight_rejects_origin_tuple_and_sequence_gaps(tmp_path: Path) -> None:
    collision = _enveloped("ad:00000000000000000000000002", 1)
    path, digest = _write_manifest(tmp_path, [("source-a", [_enveloped("ad:00000000000000000000000001", 1), collision])])
    with pytest.raises(SourcePreflightError, match="producer origin tuple collision"):
        preflight(path, digest)

    path, digest = _write_manifest(tmp_path, [("source-a", [_enveloped("ad:00000000000000000000000001", 1), _enveloped("ad:00000000000000000000000003", 3)])])
    with pytest.raises(SourcePreflightError, match="origin sequence gap"):
        preflight(path, digest)

    path, digest = _write_manifest(tmp_path, [("source-a", [_enveloped("ad:00000000000000000000000004", 2)])])
    with pytest.raises(SourcePreflightError, match="origin sequence gap"):
        preflight(path, digest)


def test_preflight_rejects_idempotency_collision_and_changed_frozen_file(tmp_path: Path) -> None:
    first = _legacy("ad:00000000000000000000000001")
    second = _legacy("ad:00000000000000000000000002")
    first["idempotency_key"] = second["idempotency_key"] = "same-key"
    path, digest = _write_manifest(tmp_path, [("source-a", [first, second])])
    with pytest.raises(SourcePreflightError, match="idempotency key collision"):
        preflight(path, digest)

    malformed_key = _legacy("ad:00000000000000000000000004")
    malformed_key["idempotency_key"] = None
    path, digest = _write_manifest(tmp_path, [("source-a", [malformed_key])])
    with pytest.raises(SourcePreflightError, match="idempotency_key must be a non-empty string"):
        preflight(path, digest)

    path, digest = _write_manifest(tmp_path, [("source-a", [_legacy("ad:00000000000000000000000003")])])
    (tmp_path / "private-fixture-0.ndjson").write_bytes(b"changed\n")
    with pytest.raises(SourcePreflightError, match="frozen source changed"):
        preflight(path, digest)


def test_cli_is_json_read_only_and_requires_external_manifest_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, digest = _write_manifest(tmp_path, [("source-a", [_legacy("ad:00000000000000000000000001")])])
    missing_db = tmp_path / "must-not-be-created.db"
    monkeypatch.setenv("AUDITCTL_DB", str(missing_db))
    runner = CliRunner()

    output = runner.invoke(cli, ["source-preflight", "--manifest", str(path), "--manifest-sha256", digest])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["qualification"] == "source-only-qualified"
    assert "private-fixture-0.ndjson" not in output.output
    assert not missing_db.exists()

    refused = runner.invoke(cli, ["source-preflight", "--manifest", str(path), "--manifest-sha256", "0" * 64])
    assert refused.exit_code != 0
    assert "manifest differs from its frozen SHA-256" in refused.output


def test_preflight_rejects_malformed_envelope_and_duplicate_json_keys(tmp_path: Path) -> None:
    partial = _legacy("ad:00000000000000000000000001")
    partial["origin_stream_id"] = STREAM
    path, digest = _write_manifest(tmp_path, [("source-a", [partial])])
    with pytest.raises(SourcePreflightError, match="invalid Auditctl event"):
        preflight(path, digest)

    raw = b'{"id":"ad:00000000000000000000000001","id":"ad:00000000000000000000000002"}\n'
    source = tmp_path / "duplicate-fields.ndjson"
    source.write_bytes(raw)
    manifest = {
        "schema_version": "auditctl-source-snapshot-manifest/v1",
        "sources": [{"source_id": "source-a", "source_repo_id": "audit-fixture", "scope": "committed-shard",
                     "path": source.name, "byte_count": len(raw), "line_count": 1,
                     "sha256": hashlib.sha256(raw).hexdigest()}],
    }
    raw_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / "duplicate-manifest.json"
    manifest_path.write_bytes(raw_manifest)
    with pytest.raises(SourcePreflightError, match="duplicate JSON object key"):
        preflight(manifest_path, hashlib.sha256(raw_manifest).hexdigest())


def test_preflight_refuses_fifo_manifest_and_source_without_blocking(tmp_path: Path) -> None:
    event = _legacy("ad:00000000000000000000000001")
    source_raw = json.dumps(event, separators=(",", ":")).encode() + b"\n"
    fifo_source = tmp_path / "source.fifo"
    os.mkfifo(fifo_source)
    manifest = {
        "schema_version": "auditctl-source-snapshot-manifest/v1",
        "sources": [{"source_id": "source-a", "source_repo_id": "audit-fixture", "scope": "committed-shard",
                     "path": fifo_source.name, "byte_count": len(source_raw), "line_count": 1,
                     "sha256": hashlib.sha256(source_raw).hexdigest()}],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest_raw)
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    with pytest.raises(SourcePreflightError, match="regular file"):
        preflight(manifest_path, manifest_digest)

    manifest_path.unlink()
    os.mkfifo(manifest_path)
    with pytest.raises(SourcePreflightError, match="regular file"):
        preflight(manifest_path, manifest_digest)

    manifest_path.unlink()
    regular_source = tmp_path / "regular-source.ndjson"
    regular_source.write_bytes(source_raw)
    symlink_source = tmp_path / "symlink-source.ndjson"
    symlink_source.symlink_to(regular_source)
    manifest = {
        "schema_version": "auditctl-source-snapshot-manifest/v1",
        "sources": [{"source_id": "source-b", "source_repo_id": "audit-fixture", "scope": "committed-shard",
                     "path": symlink_source.name, "byte_count": len(source_raw), "line_count": 1,
                     "sha256": hashlib.sha256(source_raw).hexdigest()}],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path.write_bytes(manifest_raw)
    with pytest.raises(SourcePreflightError, match="safe regular file"):
        preflight(manifest_path, hashlib.sha256(manifest_raw).hexdigest())
