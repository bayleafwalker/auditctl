from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

from click.testing import CliRunner
import pytest

from auditctl import attribution_listing as listing
from auditctl.cli import cli

# The source-owner CLI is a separate tool dependency, never copied into the
# audit package. CI/source verification supplies this exact reviewed checkout.
QUERY = Path(os.environ.get("AUDITCTL_TEST_ATTRIBUTION_QUERY", Path(__file__).resolve().parents[1] / "agentops-query/scripts/query_audit_attribution.py"))


@pytest.fixture
def real_query(tmp_path, monkeypatch):
    if not QUERY.is_file():
        pytest.fail("AUDITCTL_TEST_ATTRIBUTION_QUERY must name the reviewed Agentops query CLI")
    spec = importlib.util.spec_from_file_location("generic_attribution_fixture", QUERY)
    generic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generic)
    commands = tmp_path / "commands"; commands.mkdir()
    shim = commands / "agentops"
    shim.write_text(f"#!{sys.executable}\nimport runpy,sys\nassert sys.argv.pop(1)=='query-audit-attribution'\nrunpy.run_path({str(QUERY)!r},run_name='__main__')\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands))
    return generic, shim


def inputs(tmp_path, generic, *, tail=False):
    events = []
    for i in range(3):
        events.append({"id": f"ad:event{i}", "ts": f"2026-09-28T12:00:0{i}Z", "type": "workflow.session",
                       "source": "hook", "actor": "old-actor", "summary": "✓ legacy", "detail": None,
                       "refs": [], "metadata": {"project": "worktree-alias", "context": {"original_repo": "agentops"}},
                       "payload_sha256": "e" * 64, "record_class": "decision" if i == 0 else "observation",
                       "unknown_historical_field": {"preserve": True}})
    lines = [(json.dumps(event, ensure_ascii=False) + "\n").encode() for event in events]
    source = tmp_path / "events.ndjson"; source.write_bytes(b"".join(lines))
    prefix = b"".join(lines[:2])
    def source_descriptor(identity, raw, kind, start):
        value = {"source_id": identity, "source_repo_id": "agentops", "kind": kind,
                 "byte_count": len(raw), "line_count": raw.count(b"\n"), "sha256": hashlib.sha256(raw).hexdigest(), "start_line": start}
        if kind == "committed-shard": value.update(commit="a"*40, blob_id="b"*40)
        else: value["prefix_source_id"] = "prefix"
        return value
    def overlay(identity, descriptor, indices, scope):
        obj = {"schema_version": "audit-attribution-overlay/v1", "scope": scope, "sources": [descriptor],
               "entries": [{"event_id": events[i]["id"], "source_id": identity, "line": i+1,
                            "raw_line_sha256": hashlib.sha256(lines[i]).hexdigest(), "attributed_repo_id": "vuoro-cloud"} for i in indices]}
        obj["digest"] = "sha256:"+generic.overlay_digest(obj)
        path = tmp_path / (identity+".json"); path.write_text(json.dumps(obj)); return path
    first = overlay("prefix", source_descriptor("prefix", prefix, "committed-shard", 1), [0], "committed-only")
    overlays, sources = [first], [f"prefix={source}"]
    if tail:
        second = overlay("tail", source_descriptor("tail", source.read_bytes(), "uncommitted-tail", 3), [2], "protected-tail")
        overlays.append(second); sources.append(f"tail={source}")
    return events, source, overlays, sources


def args(overlays, sources, *extra):
    result = ["list", "--json"]
    for path in overlays: result += ["--attribution-overlay", str(path)]
    for source in sources: result += ["--attribution-source", source]
    return result + list(extra)


def test_real_cli_preserves_all_original_fields_and_never_opens_index(tmp_path, monkeypatch, real_query):
    generic, _ = real_query
    events, source, overlays, sources = inputs(tmp_path, generic)
    index = tmp_path / "missing-parent" / "audit.db"
    monkeypatch.setenv("AUDITCTL_DB", str(index))
    monkeypatch.setattr("auditctl.cli._open_db", lambda: pytest.fail("snapshot mode opened SQLite"))
    before = source.read_bytes()
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["mode"] == "verified-source-snapshots"
    assert report["verification_receipt"]["coverage"] == "committed-only"
    assert report["verification_receipt"]["unparsed_bytes_beyond_snapshots"] == {"prefix": True}
    assert [row["original_event"] for row in report["events"]] == events[1::-1]
    assert report["events"][1]["attribution"]["attributed_repo_id"] == "vuoro-cloud"
    assert report["events"][0]["attribution"]["mapped"] is False
    assert source.read_bytes() == before and not index.parent.exists()


def test_protected_tail_is_explicit_and_repo_filter_precedes_limit(tmp_path, monkeypatch, real_query):
    generic, _ = real_query
    events, source, overlays, sources = inputs(tmp_path, generic, tail=True)
    db = tmp_path / "existing.db"; db.write_bytes(b"unchanged index")
    wal = tmp_path / "existing.db-wal"; wal.write_bytes(b"unchanged WAL")
    monkeypatch.setenv("AUDITCTL_DB", str(db))
    before = (db.read_bytes(), wal.read_bytes(), source.read_bytes())
    result = CliRunner().invoke(cli, args(overlays, sources, "--attributed-repo", "vuoro-cloud", "--limit", "1", "--source", "hook"))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["verification_receipt"]["coverage"] == "committed-and-explicit-protected-tail"
    assert report["matched_events"] == 2 and report["returned_events"] == 1
    assert report["events"][0]["original_event"] == events[2]
    assert before == (db.read_bytes(), wal.read_bytes(), source.read_bytes())
    result = CliRunner().invoke(cli, args(overlays, sources, "--source", "vuoro-cloud"))
    assert json.loads(result.output)["events"] == []


def test_real_conflicting_or_changed_sources_refuse_without_partial_stdout(tmp_path, real_query):
    generic, _ = real_query
    _, source, overlays, sources = inputs(tmp_path, generic)
    source.write_bytes(source.read_bytes().replace(b"old-actor", b"bad-actor"))
    before = source.read_bytes()
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout
    assert source.read_bytes() == before


def test_real_identical_source_copies_are_verified(tmp_path, real_query):
    generic, _ = real_query
    _, source, overlays, sources = inputs(tmp_path, generic)
    copy = tmp_path / "copy.ndjson"; copy.write_bytes(source.read_bytes())
    result = CliRunner().invoke(cli, args(overlays, sources+[f"prefix={copy}"]))
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)["verification_receipt"]
    assert receipt["verified_copy_count"] == 2 and receipt["unique_events"] == 2


@pytest.mark.parametrize("extra", [["--attributed-repo", "vuoro"], ["--attribution-overlay", "missing"], ["--attribution-source", "x=missing"]])
def test_incomplete_options_refuse_before_index_access(monkeypatch, extra):
    monkeypatch.setattr("auditctl.cli._open_db", lambda: pytest.fail("opened index"))
    result = CliRunner().invoke(cli, ["list", "--json", *extra])
    assert result.exit_code != 0 and not result.stdout


def test_missing_installed_command_is_explicit(tmp_path, monkeypatch, real_query):
    generic, _ = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    monkeypatch.setenv("PATH", "")
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout
    assert "installed agentops command is required" in result.stderr


@pytest.mark.parametrize("fault", ["unknown-schema", "malformed", "duplicate-fields", "nonzero", "oversize", "overflow"])
def test_process_interface_failures_have_no_partial_output(tmp_path, monkeypatch, real_query, fault):
    generic, shim = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    programs = {"unknown-schema": "print('{\"schema_version\":\"other\"}')",
                "malformed": "print('no JSON')", "duplicate-fields": "print('{\"x\":1,\"x\":2}')",
                "nonzero": "print('partial');raise SystemExit(2)", "oversize": "print('x'*200000)",
                "overflow": "print('{\"x\":1e400}')"}
    shim.write_text(f"#!{sys.executable}\n"+programs[fault]+"\n")
    if fault == "oversize": monkeypatch.setattr(listing, "_output_budget", lambda *a: 32)
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout


def test_budget_is_derived_from_supplied_bytes_not_a_record_size_admission_limit(tmp_path, real_query):
    generic, _ = real_query
    _, source, overlays, sources = inputs(tmp_path, generic)
    first = listing._output_budget(overlays, sources)
    source.write_bytes(source.read_bytes()+b" "*100000)
    assert listing._output_budget(overlays, sources) == first+800000


@pytest.mark.parametrize("fault", ["unknown-root", "unknown-record", "bool-count", "coverage", "unmapped-repo", "duplicate-id", "missing-original", "count-map", "extension-count"])
def test_valid_json_invalid_contract_is_refused_before_stdout(tmp_path, real_query, fault):
    generic, shim = real_query
    _, source, overlays, sources = inputs(tmp_path, generic)
    report = generic.query([generic.load_overlay(overlays[0])], {"prefix": [source]}, include_events=True)
    if fault == "unknown-root": report["grant"] = True
    elif fault == "unknown-record": report["events"][0]["authority"] = "fake"
    elif fault == "bool-count": report["unique_events"] = True
    elif fault == "coverage": report["coverage"] = "committed-and-explicit-protected-tail"
    elif fault == "unmapped-repo": report["events"][1]["attributed_repo_id"] = "guessed-alias"
    elif fault == "duplicate-id": report["events"][1] = report["events"][0]
    elif fault == "missing-original": del report["events"][0]["original_event"]["ts"]
    elif fault == "count-map": report["attributed_counts"] = {"vuoro-cloud": 0}
    elif fault == "extension-count": report["unparsed_bytes_beyond_snapshots"]["prefix"] = 1
    shim.write_text(f"#!{sys.executable}\nprint({json.dumps(report)!r})\n")
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout


def test_snapshot_json_and_nonnegative_limit_required_before_helper(tmp_path, real_query):
    generic, shim = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    shim.write_text(f"#!{sys.executable}\nraise AssertionError('must not invoke')\n")
    options = args(overlays, sources)
    result = CliRunner().invoke(cli, [arg for arg in options if arg != "--json"])
    assert "requires --json" in result.stderr and not result.stdout
    result = CliRunner().invoke(cli, args(overlays, sources, "--limit", "-1"))
    assert "nonnegative" in result.stderr and not result.stdout


def test_default_list_does_not_consult_optional_dependency(repo_root, monkeypatch):
    monkeypatch.setattr(listing, "_invoke", lambda *a: pytest.fail("default list invoked Agentops"))
    result = CliRunner().invoke(cli, ["list", "--json"])
    assert result.exit_code == 0 and json.loads(result.output) == []


@pytest.mark.parametrize("target", ["source-fifo", "overlay-fifo", "source-directory", "source-device"])
def test_nonregular_inputs_refuse_before_spawn_without_hanging(tmp_path, monkeypatch, real_query, target):
    generic, _ = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    fifo = tmp_path / "pipe"; os.mkfifo(fifo)
    if target == "source-fifo": sources = [f"prefix={fifo}"]
    elif target == "overlay-fifo": overlays = [fifo]
    elif target == "source-directory": sources = [f"prefix={tmp_path}"]
    else: sources = ["prefix=/dev/null"]
    monkeypatch.setattr(listing, "_invoke", lambda *a: pytest.fail("spawned on ineligible input"))
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout
    assert "regular files" in result.stderr


def test_ordinary_symlink_to_regular_snapshot_supported(tmp_path, real_query):
    generic, _ = real_query
    _, source, overlays, _ = inputs(tmp_path, generic)
    link = tmp_path / "source-link"; link.symlink_to(source)
    result = CliRunner().invoke(cli, args(overlays, [f"prefix={link}"]))
    assert result.exit_code == 0, result.output


def test_stalled_child_is_killed_and_reaped_with_no_partial_output(tmp_path, monkeypatch, real_query):
    generic, shim = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    pidfile = tmp_path / "child.pid"
    shim.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\nprint('partial',flush=True)\ntime.sleep(30)\n")
    monkeypatch.setenv("AUDITCTL_ATTRIBUTION_TIMEOUT_SECONDS", "2")
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout
    assert "reporting deadline" in result.stderr
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    with pytest.raises(ChildProcessError): os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "3601", "not-a-number"])
def test_invalid_local_deadline_is_refused(tmp_path, monkeypatch, real_query, value):
    generic, _ = real_query
    _, _, overlays, sources = inputs(tmp_path, generic)
    monkeypatch.setenv("AUDITCTL_ATTRIBUTION_TIMEOUT_SECONDS", value)
    result = CliRunner().invoke(cli, args(overlays, sources))
    assert result.exit_code != 0 and not result.stdout
