from __future__ import annotations

import uuid
from datetime import datetime, timezone

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int) -> str:
    chars: list[str] = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_event_id(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    millis = int(now.timestamp() * 1000)
    randomness = uuid.uuid4().int & ((1 << 80) - 1)
    return f"ad:{_encode_crockford((millis << 80) | randomness)}"

