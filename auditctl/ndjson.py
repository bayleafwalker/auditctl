from __future__ import annotations

import fcntl
import glob
import json
import os
from pathlib import Path
from typing import Iterator

from .validation import canonical_json, validate_event_object


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        line = (canonical_json(event) + "\n").encode("utf-8")
        os.write(fd, line)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def resolve_inputs(raw_path: str) -> list[Path]:
    path = Path(raw_path).expanduser()
    if any(char in raw_path for char in "*?["):
        return [Path(p) for p in sorted(glob.glob(raw_path))]
    if path.is_dir():
        return sorted(path.glob("events-*.ndjson"))
    return [path]


def read_events(paths: list[Path]) -> Iterator[dict]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
                try:
                    yield validate_event_object(raw)
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_number}: {exc}") from exc

