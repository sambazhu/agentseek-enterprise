#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agentseek_enterprise.observability import EnterpriseEventWriter, EnterpriseObservabilitySettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a single AgentSeek Enterprise event to Langfuse.")
    parser.add_argument("--env-file", default=os.environ.get("AGENTSEEK_ENV_FILE", ".env"))
    parser.add_argument("--event", default="langfuse_probe")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    env_path = Path(args.env_file).expanduser()
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    _load_env_file(env_path)

    settings = EnterpriseObservabilitySettings.from_env(project_root=env_path.parent)
    writer = EnterpriseEventWriter(settings)
    writer.emit(
        args.event,
        status="probe",
        component="agentseek-enterprise",
        project_root=str(env_path.parent),
    )
    writer.wait_for_langfuse(args.timeout)
    status = writer.langfuse_status()
    result = {
        "event": args.event,
        "events_log_path": str(settings.events_log_path),
        "langfuse_enabled": settings.langfuse_enabled,
        "langfuse_host_configured": bool(settings.langfuse_host),
        "langfuse_status": status,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not settings.langfuse_enabled:
        print("Langfuse is disabled. Set AGENTSEEK_LANGFUSE_ENABLED=true before running this probe.")
        return 2
    if status.get("status") != "sent":
        return 3
    return 0


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
