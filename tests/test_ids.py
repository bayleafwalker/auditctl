from __future__ import annotations

import re
from datetime import datetime, timezone

from auditctl.ids import new_event_id


def test_event_ids_match_contract() -> None:
    event_id = new_event_id(datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc))
    assert re.match(r"^ad:[0-9A-HJKMNP-TV-Z]{26}$", event_id)


def test_event_ids_preserve_injected_time_order() -> None:
    first = new_event_id(datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc))
    second = new_event_id(datetime(2026, 4, 26, 10, 1, tzinfo=timezone.utc))
    assert first < second

