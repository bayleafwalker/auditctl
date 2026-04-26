from __future__ import annotations

from collections import defaultdict


def render_text(events: list[dict]) -> str:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        by_day[event["ts"][:10]].append(event)

    lines: list[str] = []
    for day in sorted(by_day):
        if lines:
            lines.append("")
        lines.append(f"## {day}")
        lines.append("")
        for event in by_day[day]:
            time = event["ts"][11:19]
            lines.append(
                f"- {time} {event['type']} {event['source']} {event['actor']}: {event['summary']}"
            )
            if event.get("refs"):
                lines.append(f"  refs: {', '.join(event['refs'])}")
    return "\n".join(lines)

