#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_EVENTS_PATH = "./runtime/enterprise-events.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize AgentSeek Enterprise runtime events.")
    parser.add_argument("--path", default=os.environ.get("AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH", DEFAULT_EVENTS_PATH))
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--event", action="append", default=[], help="Limit to one or more event names.")
    args = parser.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"No enterprise event log found: {path}")
        return 1

    cutoff = datetime.now(UTC) - timedelta(hours=max(args.since_hours, 0.0))
    selected_events = set(args.event or [])
    events = [
        event
        for event in _read_events(path)
        if _event_timestamp(event) >= cutoff and (not selected_events or event.get("event") in selected_events)
    ]
    _print_summary(events, path=path, since_hours=args.since_hours)
    return 0


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _event_timestamp(event: dict[str, Any]) -> datetime:
    raw = str(event.get("ts") or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _print_summary(events: list[dict[str, Any]], *, path: Path, since_hours: float) -> None:  # noqa: C901
    print(f"Enterprise events: {path}")
    print(f"Window: last {since_hours:g} hour(s)")
    print(f"Total events: {len(events)}")
    if not events:
        return

    event_counts = Counter(str(event.get("event") or "unknown") for event in events)
    status_counts = Counter(str(event.get("status") or "n/a") for event in events)
    employees = {
        str(event[key])
        for event in events
        for key in ("employee_key", "user_key", "session_key", "scope_key")
        if event.get(key)
    }
    print(f"Active hashed principals: {len(employees)}")

    print("\nTop events")
    for name, count in event_counts.most_common(12):
        print(f"- {name}: {count}")

    print("\nStatuses")
    for status, count in status_counts.most_common():
        print(f"- {status}: {count}")

    mcp_counts: Counter[str] = Counter()
    for event in events:
        if str(event.get("event") or "").startswith("mcp_"):
            key = f"{event.get('server_name', 'unknown')}/{event.get('tool_name', 'unknown')}"
            mcp_counts[key] += 1
    if mcp_counts:
        print("\nMCP tools")
        for tool_ref, count in mcp_counts.most_common(12):
            print(f"- {tool_ref}: {count}")

    durations: dict[str, list[int]] = defaultdict(list)
    for event in events:
        value = event.get("duration_ms")
        if isinstance(value, int | float):
            durations[str(event.get("event") or "unknown")].append(int(value))
    if durations:
        print("\nDurations")
        for name, values in sorted(durations.items()):
            print(f"- {name}: count={len(values)} avg={round(statistics.mean(values))}ms p95={_p95(values)}ms")


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
