"""Bounded, offline validation for source-linked semantic observations.

This module does not call a model, fetch source material, schedule work, or
change source records. An out-of-process consumer gives Auditctl a hash-pinned
result and the frozen Auditctl source manifest that bounds its evidence basis.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .validation import EVENT_ID_RE, canonical_payload_json, validate_timestamp


SEMANTIC_SCHEMA = "semantic-observation/v1"
SEMANTIC_EVENT_TYPE = "semantic.observation"
SEMANTIC_SOURCE = "semantic-consumer"
SEMANTIC_ACTOR = "auditctl-semantic-consumer"
MAX_RESULT_BYTES = 64 * 1024
MAX_CASE_ID_BYTES = 128
MAX_WORK_REF_BYTES = 256
MAX_STATEMENT_BYTES = 512
MAX_PROVENANCE_BYTES = 256
MAX_PROFILE_BYTES = 128
MAX_TOKEN_COUNT = 1_000_000_000
MAX_COST_MICROUSD = 1_000_000_000_000

_HEX = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
_ARTIFACT_SHA256 = re.compile(r"^artifact:sha256:([0-9a-f]{64})$")
_BILLING_SOURCES = {"local", "provider-billed", "subscription", "unknown", "not-applicable"}
_RESULT_FIELDS = {
    "schema_version",
    "case_id",
    "source_basis_ref",
    "source_basis_sha256",
    "interpretation_key",
    "work_ref",
    "observed_work_revision",
    "statement",
    "kind",
    "producer_model",
    "profile",
    "prompt_or_recipe_digest",
    "extracted_at",
    "analysis_generation",
    "supersedes",
    "applicability",
    "usage",
}
_USAGE_FIELDS = {
    "status",
    "input_tokens",
    "output_tokens",
    "cost_microusd",
    "billing_source",
}


class SemanticObservationError(ValueError):
    """An enrichment result is malformed, unbounded, or not tied to its basis."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticObservationError("duplicate JSON object key")
        result[key] = value
    return result


def _json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(
                SemanticObservationError("non-standard JSON number")
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, SemanticObservationError):
            raise
        raise SemanticObservationError(f"invalid {label} JSON") from error


def _read_regular_file(path: Path, limit: int) -> bytes:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise SemanticObservationError("platform lacks safe no-follow/nonblocking file opens")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SemanticObservationError("input path must name a regular file")
        if metadata.st_size > limit:
            raise SemanticObservationError("input file exceeds its byte bound")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise SemanticObservationError("input file exceeds its byte bound")
        return raw
    except SemanticObservationError:
        raise
    except OSError as error:
        raise SemanticObservationError("input file is unavailable or is not a safe regular file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_result(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    """Read one exact, bounded enrichment result without following symlinks."""
    if type(expected_sha256) is not str or not _HEX.fullmatch(expected_sha256):
        raise SemanticObservationError("expected result SHA-256 must be lowercase hex")
    raw = _read_regular_file(path, MAX_RESULT_BYTES)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise SemanticObservationError("result differs from its frozen SHA-256")
    result = _json(raw, label="semantic observation")
    if type(result) is not dict:
        raise SemanticObservationError("semantic observation must be a JSON object")
    try:
        canonical = canonical_payload_json(result).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise SemanticObservationError("semantic observation cannot be canonically encoded") from error
    if raw != canonical:
        raise SemanticObservationError("semantic observation must use canonical UTF-8 JSON bytes")
    return result, actual


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != fields:
        raise SemanticObservationError(f"invalid {label} fields")
    return value


def _bounded_string(value: Any, label: str, max_bytes: int) -> str:
    if type(value) is not str or not value:
        raise SemanticObservationError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise SemanticObservationError(f"{label} is not valid UTF-8") from error
    if len(encoded) > max_bytes:
        raise SemanticObservationError(f"{label} exceeds the {max_bytes}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SemanticObservationError(f"{label} contains a control character")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise SemanticObservationError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _validate_usage(value: Any, kind: str) -> dict[str, Any]:
    usage = _exact_fields(value, _USAGE_FIELDS, "usage")
    status = usage["status"]
    billing_source = usage["billing_source"]
    if type(status) is not str or status not in {"reported", "unknown", "not-applicable"}:
        raise SemanticObservationError("usage.status must be reported, unknown, or not-applicable")
    if type(billing_source) is not str or billing_source not in _BILLING_SOURCES:
        raise SemanticObservationError("usage.billing_source is unsupported")
    for field in ("input_tokens", "output_tokens", "cost_microusd"):
        number = usage[field]
        maximum = MAX_COST_MICROUSD if field == "cost_microusd" else MAX_TOKEN_COUNT
        if number is not None and (type(number) is not int or number < 0 or number > maximum):
            raise SemanticObservationError(f"usage.{field} must be null or a bounded non-negative integer")
    if status == "reported":
        if usage["input_tokens"] is None or usage["output_tokens"] is None:
            raise SemanticObservationError("reported usage requires input and output token counts")
        if billing_source not in {"local", "provider-billed", "subscription"}:
            raise SemanticObservationError("reported usage requires a known billing source")
    elif status == "unknown":
        if any(usage[field] is not None for field in ("input_tokens", "output_tokens", "cost_microusd")):
            raise SemanticObservationError("unknown usage values must be null")
        if billing_source != "unknown":
            raise SemanticObservationError("unknown usage requires billing_source=unknown")
    else:
        if kind != "deterministic-derived":
            raise SemanticObservationError("not-applicable usage is only valid for deterministic derivation")
        if any(usage[field] is not None for field in ("input_tokens", "output_tokens", "cost_microusd")):
            raise SemanticObservationError("not-applicable usage values must be null")
        if billing_source != "not-applicable":
            raise SemanticObservationError("not-applicable usage requires billing_source=not-applicable")
    return usage


def interpretation_key(result: dict[str, Any]) -> str:
    """Return the stable identity of one requested analysis intent."""
    intent = {
        "analysis_generation": result["analysis_generation"],
        "case_id": result["case_id"],
        "kind": result["kind"],
        "producer_model": result["producer_model"],
        "profile": result["profile"],
        "prompt_or_recipe_digest": result["prompt_or_recipe_digest"],
        "source_basis_ref": result["source_basis_ref"],
        "source_basis_sha256": result["source_basis_sha256"],
        "work_ref": result["work_ref"],
        "observed_work_revision": result["observed_work_revision"],
    }
    digest = hashlib.sha256(
        b"auditctl-semantic-interpretation-key/v1\0"
        + canonical_payload_json(intent).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def validate_result(result: Any, manifest_sha256: str) -> dict[str, Any]:
    """Validate a semantic result against a verified source snapshot manifest."""
    result = _exact_fields(result, _RESULT_FIELDS, "semantic observation")
    if result["schema_version"] != SEMANTIC_SCHEMA:
        raise SemanticObservationError("unsupported semantic observation schema")

    _bounded_string(result["case_id"], "case_id", MAX_CASE_ID_BYTES)
    basis_ref = result["source_basis_ref"]
    basis_digest = _digest(result["source_basis_sha256"], "source_basis_sha256")
    artifact_match = _ARTIFACT_SHA256.fullmatch(basis_ref) if type(basis_ref) is str else None
    if artifact_match is None or artifact_match.group(1) != manifest_sha256:
        raise SemanticObservationError("source_basis_ref must identify the verified manifest bytes")
    if basis_digest != f"sha256:{manifest_sha256}":
        raise SemanticObservationError("source_basis_sha256 differs from the verified manifest")

    _digest(result["interpretation_key"], "interpretation_key")
    work_ref = _bounded_string(result["work_ref"], "work_ref", MAX_WORK_REF_BYTES)
    if not work_ref.startswith("wi:"):
        raise SemanticObservationError("work_ref must use the wi: reference prefix")
    revision = result["observed_work_revision"]
    if type(revision) is not int or revision < 1:
        raise SemanticObservationError("observed_work_revision must be a positive integer")

    statement = _bounded_string(result["statement"], "statement", MAX_STATEMENT_BYTES)
    if "\n" in statement or "\r" in statement:
        raise SemanticObservationError("statement must be one line")
    kind = result["kind"]
    if type(kind) is not str or kind not in {"model-inferred", "deterministic-derived"}:
        raise SemanticObservationError("kind must be model-inferred or deterministic-derived")

    producer_model = result["producer_model"]
    if kind == "model-inferred":
        producer_model = _bounded_string(producer_model, "producer_model", MAX_PROVENANCE_BYTES)
        if "/" not in producer_model or "@" not in producer_model:
            raise SemanticObservationError("producer_model must include provider/model@revision")
    elif producer_model is not None:
        producer_model = _bounded_string(producer_model, "producer_model", MAX_PROVENANCE_BYTES)
        if not producer_model.startswith("deterministic/") or "@" not in producer_model:
            raise SemanticObservationError("deterministic producer_model must identify extractor@revision")

    _bounded_string(result["profile"], "profile", MAX_PROFILE_BYTES)
    _digest(result["prompt_or_recipe_digest"], "prompt_or_recipe_digest")
    extracted_at = _bounded_string(result["extracted_at"], "extracted_at", 32)
    try:
        validate_timestamp(extracted_at)
    except (AttributeError, TypeError) as error:
        raise SemanticObservationError("extracted_at must be an ISO UTC timestamp") from error
    generation = result["analysis_generation"]
    if type(generation) is not int or generation < 1:
        raise SemanticObservationError("analysis_generation must be a positive integer")
    supersedes = result["supersedes"]
    if supersedes is not None and (type(supersedes) is not str or not EVENT_ID_RE.fullmatch(supersedes)):
        raise SemanticObservationError("supersedes must be null or an Auditctl event reference")
    if (generation == 1) != (supersedes is None):
        raise SemanticObservationError(
            "generation 1 requires supersedes=null; later generations require a prior event reference"
        )
    if result["applicability"] != "exact-basis-and-work-revision":
        raise SemanticObservationError("applicability must be exact-basis-and-work-revision")
    _validate_usage(result["usage"], kind)

    expected_key = interpretation_key(result)
    if result["interpretation_key"] != expected_key:
        raise SemanticObservationError("interpretation_key does not match the analysis intent")
    return result


def validate_semantic_event(event: dict[str, Any]) -> None:
    """Validate the semantic publisher's fixed ordinary-observation envelope."""
    if event.get("type") != SEMANTIC_EVENT_TYPE and event.get("source") != SEMANTIC_SOURCE:
        return
    if event.get("type") != SEMANTIC_EVENT_TYPE or event.get("source") != SEMANTIC_SOURCE:
        raise SemanticObservationError("semantic observation type and source must be used together")
    if event.get("actor") != SEMANTIC_ACTOR:
        raise SemanticObservationError("semantic observation actor is fixed to the consumer process")
    if event.get("detail") is not None:
        raise SemanticObservationError("semantic observation detail must remain empty")
    if event.get("record_class", "observation") != "observation":
        raise SemanticObservationError("semantic enrichment is an observation, not a decision")

    metadata = _exact_fields(
        event.get("metadata"),
        {"semantic_observation", "semantic_result_sha256", "basis_revision", "correlation_id"},
        "semantic metadata",
    )
    semantic = metadata["semantic_observation"]
    if type(semantic) is not dict:
        raise SemanticObservationError("semantic_observation must be an object")
    basis_ref = semantic.get("source_basis_ref")
    basis_match = _ARTIFACT_SHA256.fullmatch(basis_ref) if type(basis_ref) is str else None
    if basis_match is None:
        raise SemanticObservationError("source_basis_ref must be content-addressed")
    validate_result(semantic, basis_match.group(1))
    recorded_result_digest = _digest(metadata["semantic_result_sha256"], "semantic_result_sha256")
    canonical_result = canonical_payload_json(semantic).encode("utf-8")
    actual_result_digest = f"sha256:{hashlib.sha256(canonical_result).hexdigest()}"
    if recorded_result_digest != actual_result_digest:
        raise SemanticObservationError("semantic_result_sha256 differs from the canonical result bytes")
    if event.get("summary") != semantic["statement"]:
        raise SemanticObservationError("summary must equal semantic_observation.statement")
    if event.get("ts") != semantic["extracted_at"]:
        raise SemanticObservationError("audit timestamp must equal semantic extraction time")
    if event.get("refs") != [semantic["work_ref"]]:
        raise SemanticObservationError("semantic observation refs must contain exactly its work_ref")
    if metadata["basis_revision"] != str(semantic["observed_work_revision"]):
        raise SemanticObservationError("basis_revision must match observed_work_revision")
    if metadata["correlation_id"] != semantic["case_id"]:
        raise SemanticObservationError("correlation_id must match case_id")


def build_event(result: dict[str, Any], result_sha256: str) -> dict[str, Any]:
    """Project a validated result into Auditctl's existing observation envelope input."""
    if type(result_sha256) is not str or not _HEX.fullmatch(result_sha256):
        raise SemanticObservationError("result SHA-256 must be lowercase hex")
    return {
        "ts": result["extracted_at"],
        "type": SEMANTIC_EVENT_TYPE,
        "actor": SEMANTIC_ACTOR,
        "summary": result["statement"],
        "detail": None,
        "refs": [result["work_ref"]],
        "source": SEMANTIC_SOURCE,
        "metadata": {
            "semantic_observation": result,
            "semantic_result_sha256": f"sha256:{result_sha256}",
            "basis_revision": str(result["observed_work_revision"]),
            "correlation_id": result["case_id"],
        },
    }
