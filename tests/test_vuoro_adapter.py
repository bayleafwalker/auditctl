from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from auditctl.central import IngestGapError, IngestReceipt
from auditctl.vuoro_adapter import (
    READ_AUTHORITY,
    SUBMIT_AUTHORITY,
    VuoroAuditAdapter,
    catalog_operation_specs,
)


class _ConnectionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


@dataclass(frozen=True)
class _Definition:
    values: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.values["name"])


class _Registry:
    def __init__(self) -> None:
        self.operations: dict[str, tuple[_Definition, Any]] = {}

    def register(self, definition: _Definition, handler: Any) -> None:
        self.operations[definition.name] = (definition, handler)


class _Rejected(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _reject(code: str, message: str, http_status: int) -> BaseException:
    return _Rejected(code, message, http_status)


def _definition(**values: Any) -> _Definition:
    return _Definition(values)


def _adapter() -> VuoroAuditAdapter:
    return VuoroAuditAdapter(
        connection_factory=_ConnectionContext,
        schema="audit_test",
        rejection_factory=_reject,
    )


def _receipt(*, duplicate: bool = False) -> IngestReceipt:
    return IngestReceipt(
        receipt_id="c780bd9f-47c1-47fb-b4c6-21a90bf5e241",
        ingest_offset=1,
        origin_stream_id="40b89732-b2c7-4f60-98c2-199a960c2a20",
        origin_seq=1,
        event_id="ad:00000000000000000000000001",
        record_sha256="1" * 64,
        first_received_at="2026-07-21T12:00:01Z",
        last_seen_at="2026-07-21T12:00:01Z",
        duplicate_count=int(duplicate),
        duplicate=duplicate,
    )


def test_catalog_registers_owned_operations_with_explicit_transport_semantics() -> None:
    registry = _Registry()

    _adapter().register(registry, operation_definition_factory=_definition)

    assert set(registry.operations) == {
        "audit.observation.submit",
        "audit.receipt.lookup",
        "audit.observation.list",
        "audit.stream.status",
        "audit.schema.compatibility",
    }
    submit = registry.operations["audit.observation.submit"][0].values
    read = registry.operations["audit.observation.list"][0].values
    assert submit["owning_domain"] == "audit"
    assert submit["required_authority"] == SUBMIT_AUTHORITY
    assert submit["execution_semantics"] == "write"
    assert submit["idempotency"] == "required"
    assert read["required_authority"] == READ_AUTHORITY
    assert read["execution_semantics"] == "read"
    assert read["idempotency"] == "not-allowed"
    assert read["input_schema"]["properties"]["limit"]["maximum"] == 100
    for definition, _handler in registry.operations.values():
        assert definition.values["input_schema"]["$schema"].endswith("2020-12/schema")
        assert definition.values["result_schema"]["$schema"].endswith("2020-12/schema")
        assert definition.values["required_client_schema_features"] == [
            "json-schema-draft-2020-12"
        ]


def test_catalog_specs_are_fresh_data_and_do_not_expose_handler_objects() -> None:
    first = catalog_operation_specs()
    first[0]["input_schema"]["properties"].clear()

    second = catalog_operation_specs()

    assert "observation" in second[0]["input_schema"]["properties"]
    assert all(callable(spec["_handler_name"]) is False for spec in second)


def test_local_capture_import_path_does_not_load_served_dependencies() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import auditctl.cli; "
                "assert 'vuoro_service' not in sys.modules; "
                "assert 'psycopg' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_submit_requires_context_key_and_translates_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter()
    with pytest.raises(_Rejected) as missing:
        adapter.submit_observation(
            {"observation": {}}, SimpleNamespace(idempotency_key=None)
        )
    assert (missing.value.code, missing.value.http_status) == (
        "audit-idempotency-key-required",
        400,
    )

    def gap(_conn: object, _event: dict[str, Any], *, schema: str) -> IngestReceipt:
        assert schema == "audit_test"
        raise IngestGapError("40b89732-b2c7-4f60-98c2-199a960c2a20", 2, 3)

    monkeypatch.setattr("auditctl.vuoro_adapter.ingest_observation", gap)
    with pytest.raises(_Rejected) as rejected:
        adapter.submit_observation(
            {"observation": {}}, SimpleNamespace(idempotency_key="event-3")
        )
    assert (rejected.value.code, rejected.value.http_status) == (
        "audit-origin-sequence-gap",
        409,
    )
    assert "expected sequence 2, received 3" in str(rejected.value)


def test_uuid_arguments_fail_as_domain_input_instead_of_database_errors() -> None:
    adapter = _adapter()

    with pytest.raises(_Rejected) as receipt:
        adapter.lookup_receipt({"receipt_id": "not-a-uuid"}, SimpleNamespace())
    with pytest.raises(_Rejected) as stream:
        adapter.stream_status(
            {"origin_stream_id": "not-a-uuid"}, SimpleNamespace()
        )
    with pytest.raises(_Rejected) as read:
        adapter.read_observations(
            {"origin_stream_id": "not-a-uuid"}, SimpleNamespace()
        )

    assert (receipt.value.code, receipt.value.http_status) == (
        "audit-receipt-lookup-invalid",
        422,
    )
    assert (stream.value.code, stream.value.http_status) == (
        "audit-stream-status-invalid",
        422,
    )
    assert (read.value.code, read.value.http_status) == ("audit-read-invalid", 422)


def test_handlers_return_receipts_and_json_safe_bounded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    monkeypatch.setattr(
        "auditctl.vuoro_adapter.ingest_observation",
        lambda _conn, _event, *, schema: _receipt(),
    )
    monkeypatch.setattr(
        "auditctl.vuoro_adapter.list_observations",
        lambda _conn, **_values: [
            {
                "ingest_offset": 7,
                "origin_stream_id": UUID("40b89732-b2c7-4f60-98c2-199a960c2a20"),
                "ingested_at": datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
                "payload": {"nested": [UUID("c780bd9f-47c1-47fb-b4c6-21a90bf5e241")]},
            }
        ],
    )

    submitted = adapter.submit_observation(
        {"observation": {}}, SimpleNamespace(idempotency_key="event-1")
    )
    rows = adapter.read_observations(
        {"after_offset": 4, "limit": 10}, SimpleNamespace()
    )

    assert submitted["receipt"]["receipt_id"] == _receipt().receipt_id
    assert rows == {
        "observations": [
            {
                "ingest_offset": 7,
                "origin_stream_id": "40b89732-b2c7-4f60-98c2-199a960c2a20",
                "ingested_at": "2026-07-21T12:00:00Z",
                "payload": {"nested": ["c780bd9f-47c1-47fb-b4c6-21a90bf5e241"]},
            }
        ],
        "watermark": 7,
    }
