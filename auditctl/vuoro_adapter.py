"""Vuoro protocol-v1 adapter for the audit observation application core.

The adapter intentionally depends on the small registry surface at composition
time instead of importing the Vuoro service from auditctl's local capture path.
This keeps SQLite/NDJSON capture usable when the served-substrate extras are not
installed and keeps all admission rules in :mod:`auditctl.central`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, NoReturn, Protocol
from uuid import UUID

from .central import (
    IngestConflictError,
    IngestGapError,
    get_receipt,
    get_stream_status,
    ingest_observation,
    list_observations,
)
from .central_schema import SchemaCompatibilityError, check_compatibility

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DOMAIN_NAME = "audit"
DOMAIN_API_VERSION = "audit/v1"
SCHEMA_FEATURES = ["json-schema-draft-2020-12"]
READ_AUTHORITY = "audit.observation.read"
SUBMIT_AUTHORITY = "audit.observation.submit"

ConnectionFactory = Callable[[], AbstractContextManager[Any]]
OperationDefinitionFactory = Callable[..., Any]
RejectionFactory = Callable[[str, str, int], BaseException]


class CatalogRegistry(Protocol):
    """The structural subset of Vuoro's registry used during composition."""

    def register(self, definition: Any, handler: Callable[[Any, Any], Any]) -> None:
        ...


def _object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = required
    return schema


_NULLABLE_TEXT = {"type": ["string", "null"], "minLength": 1}
_UUID = {"type": "string", "format": "uuid"}
_TIMESTAMP = {"type": "string", "format": "date-time"}
_DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_EVENT_ID = {"type": "string", "pattern": "^ad:[0-9A-HJKMNP-TV-Z]{26}$"}

_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "detail": _NULLABLE_TEXT,
        "refs": {"type": "array", "items": {"type": "string"}},
        "source": {"type": "string", "minLength": 1},
        "metadata": {"type": "object"},
    },
    "required": ["summary", "detail", "refs", "source", "metadata"],
    "additionalProperties": False,
}

_OBSERVATION_PROPERTIES = {
    "id": _EVENT_ID,
    "ts": _TIMESTAMP,
    "type": {"type": "string", "minLength": 1},
    "actor": {"type": "string", "minLength": 1},
    "summary": {"type": "string", "minLength": 1},
    "detail": _NULLABLE_TEXT,
    "refs": {"type": "array", "items": {"type": "string"}},
    "source": {"type": "string", "minLength": 1},
    "metadata": {"type": "object"},
    "created_at": _TIMESTAMP,
    "event_id": _EVENT_ID,
    "schema_version": {"const": 1},
    "record_class": {"const": "observation"},
    "origin_stream_id": _UUID,
    "origin_seq": {"type": "integer", "minimum": 1},
    "event_type": {"type": "string", "minLength": 1},
    "runtime_session_id": _NULLABLE_TEXT,
    "occurred_at": _TIMESTAMP,
    "basis_revision": _NULLABLE_TEXT,
    "correlation_id": _NULLABLE_TEXT,
    "causation_id": _NULLABLE_TEXT,
    "payload": _PAYLOAD_SCHEMA,
    "payload_sha256": _DIGEST,
}
_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": _OBSERVATION_PROPERTIES,
    "required": [name for name in _OBSERVATION_PROPERTIES if name != "detail"],
    "additionalProperties": False,
}

_RECEIPT_PROPERTIES = {
    "receipt_id": _UUID,
    "ingest_offset": {"type": "integer", "minimum": 1},
    "origin_stream_id": _UUID,
    "origin_seq": {"type": "integer", "minimum": 1},
    "event_id": _EVENT_ID,
    "record_sha256": _DIGEST,
    "first_received_at": _TIMESTAMP,
    "last_seen_at": _TIMESTAMP,
    "duplicate_count": {"type": "integer", "minimum": 0},
    "duplicate": {"type": "boolean"},
}
_RECEIPT_SCHEMA = {
    "type": "object",
    "properties": _RECEIPT_PROPERTIES,
    "required": list(_RECEIPT_PROPERTIES),
    "additionalProperties": False,
}

_CENTRAL_OBSERVATION_PROPERTIES = {
    "ingest_offset": {"type": "integer", "minimum": 1},
    "origin_stream_id": _UUID,
    "origin_seq": {"type": "integer", "minimum": 1},
    "event_id": _EVENT_ID,
    "schema_version": {"const": 1},
    "record_class": {"const": "observation"},
    "event_type": {"type": "string", "minLength": 1},
    "actor": {"type": "string", "minLength": 1},
    "runtime_session_id": _NULLABLE_TEXT,
    "occurred_at": _TIMESTAMP,
    "basis_revision": _NULLABLE_TEXT,
    "correlation_id": _NULLABLE_TEXT,
    "causation_id": _NULLABLE_TEXT,
    "payload": _PAYLOAD_SCHEMA,
    "payload_sha256": _DIGEST,
    "record_sha256": _DIGEST,
    "producer_created_at": _TIMESTAMP,
    "ingested_at": _TIMESTAMP,
    "receipt_id": _UUID,
}
_CENTRAL_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": _CENTRAL_OBSERVATION_PROPERTIES,
    "required": list(_CENTRAL_OBSERVATION_PROPERTIES),
    "additionalProperties": False,
}

_COMPATIBILITY_PROPERTIES = {
    "schema": {"type": "string", "minLength": 1},
    "domain_api_version": {"const": DOMAIN_API_VERSION},
    "installed_schema_version": {"type": ["integer", "null"]},
    "minimum_schema_version": {"type": "integer", "minimum": 1},
    "maximum_schema_version": {"type": "integer", "minimum": 1},
    "current_role": {"type": "string", "minLength": 1},
    "expected_role_kind": {"const": "runtime"},
    "configured_role": {"type": ["string", "null"]},
    "compatible": {"type": "boolean"},
    "reasons": {"type": "array", "items": {"type": "string"}},
}


def _operation(
    *,
    name: str,
    input_schema: dict[str, Any],
    result_schema: dict[str, Any],
    authority: str,
    execution_semantics: str,
    idempotency: str,
    handler_name: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "owning_domain": DOMAIN_NAME,
        "input_schema": input_schema,
        "result_schema": result_schema,
        "required_authority": authority,
        "execution_semantics": execution_semantics,
        "idempotency": idempotency,
        "deprecation": {
            "deprecated": False,
            "replacement": None,
            "sunset_at": None,
        },
        "required_client_schema_features": SCHEMA_FEATURES,
        "_handler_name": handler_name,
    }


_OPERATIONS = (
    _operation(
        name="audit.observation.submit",
        input_schema=_object_schema(
            {"observation": _OBSERVATION_SCHEMA}, required=["observation"]
        ),
        result_schema=_object_schema(
            {"receipt": _RECEIPT_SCHEMA}, required=["receipt"]
        ),
        authority=SUBMIT_AUTHORITY,
        execution_semantics="write",
        idempotency="required",
        handler_name="submit_observation",
    ),
    _operation(
        name="audit.receipt.lookup",
        input_schema={
            **_object_schema(
                {"receipt_id": _UUID, "event_id": _EVENT_ID},
            ),
            "oneOf": [
                {"required": ["receipt_id"]},
                {"required": ["event_id"]},
            ],
        },
        result_schema=_object_schema(
            {"receipt": {"oneOf": [_RECEIPT_SCHEMA, {"type": "null"}]}},
            required=["receipt"],
        ),
        authority=READ_AUTHORITY,
        execution_semantics="read",
        idempotency="not-allowed",
        handler_name="lookup_receipt",
    ),
    _operation(
        name="audit.observation.list",
        input_schema=_object_schema(
            {
                "after_offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "event_type": _NULLABLE_TEXT,
                "origin_stream_id": {"oneOf": [_UUID, {"type": "null"}]},
            }
        ),
        result_schema=_object_schema(
            {
                "observations": {
                    "type": "array",
                    "items": _CENTRAL_OBSERVATION_SCHEMA,
                    "maxItems": 100,
                },
                "watermark": {"type": "integer", "minimum": 0},
            },
            required=["observations", "watermark"],
        ),
        authority=READ_AUTHORITY,
        execution_semantics="read",
        idempotency="not-allowed",
        handler_name="read_observations",
    ),
    _operation(
        name="audit.stream.status",
        input_schema=_object_schema(
            {"origin_stream_id": _UUID}, required=["origin_stream_id"]
        ),
        result_schema=_object_schema(
            {
                "origin_stream_id": _UUID,
                "highest_contiguous_seq": {"type": "integer", "minimum": 0},
                "next_expected_seq": {"type": "integer", "minimum": 1},
            },
            required=[
                "origin_stream_id",
                "highest_contiguous_seq",
                "next_expected_seq",
            ],
        ),
        authority=READ_AUTHORITY,
        execution_semantics="read",
        idempotency="not-allowed",
        handler_name="stream_status",
    ),
    _operation(
        name="audit.schema.compatibility",
        input_schema=_object_schema({}),
        result_schema=_object_schema(
            _COMPATIBILITY_PROPERTIES,
            required=list(_COMPATIBILITY_PROPERTIES),
        ),
        authority=READ_AUTHORITY,
        execution_semantics="read",
        idempotency="not-allowed",
        handler_name="schema_compatibility",
    ),
)


def catalog_operation_specs() -> tuple[dict[str, Any], ...]:
    """Return immutable-by-convention catalog data without importing Vuoro."""
    return tuple(deepcopy(spec) for spec in _OPERATIONS)


def _load_operation_definition(**values: Any) -> Any:
    try:
        from vuoro_service.contracts import OperationDefinition
    except ImportError as exc:  # pragma: no cover - depends on composed image
        raise RuntimeError(
            "registering the audit adapter requires vuoro-service in the composed runtime"
        ) from exc
    return OperationDefinition(**values)


def _load_rejection(code: str, message: str, http_status: int) -> BaseException:
    try:
        from vuoro_service.catalog import OperationRejectedError
    except ImportError as exc:  # pragma: no cover - depends on composed image
        raise RuntimeError(
            "invoking the audit adapter requires vuoro-service in the composed runtime"
        ) from exc
    return OperationRejectedError(code, message, http_status=http_status)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _uuid_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a UUID")
    try:
        UUID(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a UUID") from error
    return value


@dataclass(frozen=True)
class VuoroAuditAdapter:
    """Bind Vuoro operation handlers to an audit runtime connection factory."""

    connection_factory: ConnectionFactory
    schema: str = "audit"
    rejection_factory: RejectionFactory = _load_rejection

    def _reject(self, code: str, message: str, http_status: int) -> NoReturn:
        raise self.rejection_factory(code, message, http_status)

    def submit_observation(self, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        if not getattr(context, "idempotency_key", None):
            self._reject(
                "audit-idempotency-key-required",
                "observation submission requires an idempotency key",
                400,
            )
        try:
            with self.connection_factory() as conn:
                receipt = ingest_observation(
                    conn, arguments["observation"], schema=self.schema
                )
        except IngestGapError as error:
            self._reject(
                "audit-origin-sequence-gap",
                f"origin stream expected sequence {error.expected}, received {error.received}",
                409,
            )
        except IngestConflictError as error:
            self._reject("audit-observation-conflict", str(error), 409)
        except SchemaCompatibilityError as error:
            self._reject("audit-schema-incompatible", str(error), 503)
        except (KeyError, ValueError) as error:
            self._reject("audit-observation-invalid", str(error), 422)
        return {"receipt": receipt.to_dict()}

    def lookup_receipt(self, arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
        try:
            receipt_id = arguments.get("receipt_id")
            if receipt_id is not None:
                receipt_id = _uuid_text(receipt_id, "receipt_id")
            with self.connection_factory() as conn:
                receipt = get_receipt(
                    conn,
                    schema=self.schema,
                    receipt_id=receipt_id,
                    event_id=arguments.get("event_id"),
                )
        except SchemaCompatibilityError as error:
            self._reject("audit-schema-incompatible", str(error), 503)
        except ValueError as error:
            self._reject("audit-receipt-lookup-invalid", str(error), 422)
        return {"receipt": None if receipt is None else receipt.to_dict()}

    def read_observations(self, arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
        after_offset = arguments.get("after_offset", 0)
        try:
            origin_stream_id = arguments.get("origin_stream_id")
            if origin_stream_id is not None:
                origin_stream_id = _uuid_text(
                    origin_stream_id, "origin_stream_id"
                )
            with self.connection_factory() as conn:
                rows = list_observations(
                    conn,
                    schema=self.schema,
                    after_offset=after_offset,
                    limit=arguments.get("limit", 50),
                    event_type=arguments.get("event_type"),
                    origin_stream_id=origin_stream_id,
                )
        except SchemaCompatibilityError as error:
            self._reject("audit-schema-incompatible", str(error), 503)
        except ValueError as error:
            self._reject("audit-read-invalid", str(error), 422)
        observations = _json_value(rows)
        watermark = (
            int(observations[-1]["ingest_offset"]) if observations else after_offset
        )
        return {"observations": observations, "watermark": watermark}

    def stream_status(self, arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
        try:
            origin_stream_id = _uuid_text(
                arguments["origin_stream_id"], "origin_stream_id"
            )
            with self.connection_factory() as conn:
                status = get_stream_status(
                    conn,
                    schema=self.schema,
                    origin_stream_id=origin_stream_id,
                )
        except SchemaCompatibilityError as error:
            self._reject("audit-schema-incompatible", str(error), 503)
        except (KeyError, ValueError) as error:
            self._reject("audit-stream-status-invalid", str(error), 422)
        return asdict(status)

    def compatibility(self) -> dict[str, Any]:
        """Return the read-only state used by service startup and handshake."""
        with self.connection_factory() as conn:
            return check_compatibility(
                conn, schema=self.schema, expected_role_kind="runtime"
            ).to_dict()

    def schema_compatibility(
        self, _arguments: dict[str, Any], _context: Any
    ) -> dict[str, Any]:
        return self.compatibility()

    def register(
        self,
        registry: CatalogRegistry,
        *,
        operation_definition_factory: OperationDefinitionFactory = _load_operation_definition,
    ) -> None:
        """Register every owned operation in a protocol-v1 catalog registry."""
        for raw_spec in catalog_operation_specs():
            handler_name = raw_spec.pop("_handler_name")
            registry.register(
                operation_definition_factory(**raw_spec),
                getattr(self, handler_name),
            )


__all__ = [
    "DOMAIN_API_VERSION",
    "READ_AUTHORITY",
    "SUBMIT_AUTHORITY",
    "VuoroAuditAdapter",
    "catalog_operation_specs",
]
