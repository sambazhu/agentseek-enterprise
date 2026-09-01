#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from agentseek_wecom.durable import DurableStoreError, FailedInboxRecord, SqliteDurableMessageStore
from pydantic import SecretStr


class ReconciliationConfigError(ValueError):
    """Raised when the operator reconciliation configuration is invalid."""

    @classmethod
    def env_file_missing(cls, path: Path) -> ReconciliationConfigError:
        return cls(f"env file not found: {path}")

    @classmethod
    def sqlite_mode_required(cls) -> ReconciliationConfigError:
        return cls("AGENTSEEK_WECOM_DURABLE_MODE must be sqlite")

    @classmethod
    def sqlite_path_required(cls) -> ReconciliationConfigError:
        return cls("AGENTSEEK_WECOM_DURABLE_SQLITE_PATH is required")

    @classmethod
    def durable_secret_invalid(cls) -> ReconciliationConfigError:
        return cls("AGENTSEEK_WECOM_DURABLE_SECRET must contain at least 32 characters")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List or explicitly requeue failed WeCom inbox records without an outbox.",
    )
    parser.add_argument("--env-file", default=os.environ.get("AGENTSEEK_ENV_FILE", ".env"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-failed", action="store_true")
    action.add_argument("--requeue", metavar="INBOX_ID")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--gateway-stopped",
        action="store_true",
        help="Required acknowledgement for a requeue mutation.",
    )
    args = parser.parse_args(argv)

    try:
        env = _load_env(Path(args.env_file))
        store = _store_from_env(env, project_root=Path.cwd())
        now = datetime.now(UTC)
        if args.list_failed:
            records = store.list_failed_inbox_without_outbox(limit=args.limit)
            print(
                json.dumps({"failed_without_outbox": [_record_view(item, now, args.max_attempts) for item in records]})
            )
            return 0
        if not args.gateway_stopped:
            parser.error("--gateway-stopped is required with --requeue")
        record = store.requeue_failed_inbox(
            args.requeue,
            now=now,
            max_attempts=args.max_attempts,
        )
    except (OSError, ValueError, DurableStoreError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1

    print(
        json.dumps({
            "status": "requeued",
            "inbox_id": record.inbox_id,
            "stream_id": record.stream_id,
            "attempts": record.attempts,
            "reply_deadline": _datetime_text(record.reply_deadline),
        })
    )
    return 0


def _load_env(path: Path) -> dict[str, str]:
    resolved = path if path.is_absolute() else Path.cwd() / path
    if not resolved.is_file():
        raise ReconciliationConfigError.env_file_missing(resolved)
    env = dict(os.environ)
    for line in resolved.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = _unquote(value.strip())
    return env


def _store_from_env(env: dict[str, str], *, project_root: Path) -> SqliteDurableMessageStore:
    if env.get("AGENTSEEK_WECOM_DURABLE_MODE", "memory").strip() != "sqlite":
        raise ReconciliationConfigError.sqlite_mode_required()
    raw_path = env.get("AGENTSEEK_WECOM_DURABLE_SQLITE_PATH", "").strip()
    if not raw_path:
        raise ReconciliationConfigError.sqlite_path_required()
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    key_material = env.get("AGENTSEEK_WECOM_DURABLE_SECRET", "")
    if len(key_material) < 32:
        raise ReconciliationConfigError.durable_secret_invalid()
    return SqliteDurableMessageStore(path=path, secret=SecretStr(key_material))


def _record_view(record: FailedInboxRecord, now: datetime, max_attempts: int) -> dict[str, object]:
    if record.reply_deadline is None:
        eligibility = "reply_deadline_missing"
    elif record.reply_deadline <= now:
        eligibility = "reply_deadline_expired"
    elif record.attempts >= max_attempts:
        eligibility = "attempt_limit_reached"
    else:
        eligibility = "requeueable"
    return {
        "inbox_id": record.inbox_id,
        "stream_id": record.stream_id,
        "attempts": record.attempts,
        "reply_deadline": _datetime_text(record.reply_deadline),
        "last_error_type": record.last_error_type,
        "eligibility": eligibility,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
