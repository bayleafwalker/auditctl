from __future__ import annotations

import pytest

from auditctl.validation import parse_metadata, validate_refs, validate_timestamp


def test_invalid_ref_errors() -> None:
    with pytest.raises(ValueError, match="invalid ref"):
        validate_refs(["bad:1"])


def test_invalid_timestamp_errors() -> None:
    with pytest.raises(ValueError, match="ending in Z"):
        validate_timestamp("2026-04-26T10:00:00+00:00")


def test_metadata_must_be_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_metadata("[]")

