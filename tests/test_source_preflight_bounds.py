import hashlib
import json
from pathlib import Path

import pytest

from auditctl import source_preflight
from test_source_preflight import _legacy, _write_manifest


def test_declared_record_bound_refuses_before_opening_sources(tmp_path: Path, monkeypatch) -> None:
    path, digest = _write_manifest(tmp_path, [
        ("source-a", [_legacy("ad:00000000000000000000000001")]),
        ("source-b", [_legacy("ad:00000000000000000000000002")]),
    ])
    monkeypatch.setattr(source_preflight, "_MAX_RECORDS", 1, raising=False)

    def must_not_read_source(*args):
        pytest.fail("over-bound manifest opened a source")

    monkeypatch.setattr(source_preflight, "_read_frozen_source", must_not_read_source)
    with pytest.raises(source_preflight.SourcePreflightError, match="record bound"):
        source_preflight.preflight(path, digest)


def test_record_bound_includes_duplicate_copies_and_accepts_boundary(tmp_path: Path, monkeypatch) -> None:
    event = _legacy("ad:00000000000000000000000001", "café 東京")
    path, digest = _write_manifest(tmp_path, [("source-a", [event]), ("source-b", [event])])
    monkeypatch.setattr(source_preflight, "_MAX_RECORDS", 2, raising=False)
    result = source_preflight.preflight(path, digest)
    assert result["line_count"] == len(result["records"]) == 2
    assert result["distinct_event_count"] == result["exact_duplicate_copies"] == 1
    monkeypatch.setattr(source_preflight, "_MAX_RECORDS", 1)
    with pytest.raises(source_preflight.SourcePreflightError, match="record bound"):
        source_preflight.preflight(path, digest)


@pytest.mark.parametrize("raw", [b"one\ntwo\n", b"one\r\ntwo\r\n", b"one\rtwo\n", b"\n", b"one", b""])
def test_line_iterator_preserves_original_byte_framing(raw: bytes) -> None:
    assert list(source_preflight._source_lines(raw)) == raw.splitlines(keepends=True)


def test_preflight_preserves_mixed_line_hashes_and_byte_offsets(tmp_path: Path) -> None:
    events = [_legacy(f"ad:{index:026d}", "café 東京") for index in range(1, 4)]
    path, _ = _write_manifest(tmp_path, [("source-a", events)])
    manifest = json.loads(path.read_bytes())
    source = manifest["sources"][0]
    source_path = tmp_path / source["path"]
    lines = [
        line.rstrip(b"\n") + ending
        for line, ending in zip(source_path.read_bytes().splitlines(keepends=True), [b"\r\n", b"\r", b"\n"])
    ]
    raw = b"".join(lines)
    source_path.write_bytes(raw)
    source["byte_count"] = len(raw)
    source["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_raw = json.dumps(manifest).encode()
    path.write_bytes(manifest_raw)
    result = source_preflight.preflight(path, hashlib.sha256(manifest_raw).hexdigest())
    offset = 0
    for record, line in zip(result["records"], lines):
        assert record["byte_offset"] == offset
        assert record["raw_line_sha256"] == hashlib.sha256(line).hexdigest()
        offset += len(line)
    assert result["line_count"] == 3
