from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from agentseek_enterprise.identity.dm_staff_provider import DmStaffIdentityProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DM employee identity subprocess sidecar.")
    parser.add_argument("--oa", help="OA account / plaintext WeCom userid.")
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run as a long-lived JSON-lines worker over stdin/stdout.",
    )
    args = parser.parse_args(argv)

    # The parent process enables subprocess mode. The child must perform the
    # actual JDBC lookup in-process, otherwise it would recursively spawn itself.
    os.environ["AGENTSEEK_IDENTITY_DM_EXECUTION_MODE"] = "in_process"

    if args.server:
        return _run_server()
    if not args.oa:
        parser.error("--oa is required unless --server is set")

    try:
        context = DmStaffIdentityProvider().get_employee_context(args.oa)
    except Exception as exc:  # pragma: no cover - exercised by parent integration tests.
        _write_json(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return 1

    _write_json(
        {
            "ok": True,
            "employee_context": context.to_dict() if context is not None else None,
        }
    )
    return 0


def _run_server() -> int:
    provider = DmStaffIdentityProvider(keep_connection=True)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            _write_json(_handle_server_request(provider, line))
            sys.stdout.flush()
    finally:
        provider.close()
    return 0


def _handle_server_request(provider: DmStaffIdentityProvider, line: str) -> dict[str, Any]:
    try:
        oa_account = _oa_account_from_request(json.loads(line))
        context = _lookup_with_reconnect(provider, oa_account)
    except Exception as exc:  # pragma: no cover - exact DB failures are environment dependent.
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {
        "ok": True,
        "employee_context": context.to_dict() if context is not None else None,
    }


def _oa_account_from_request(request: Any) -> str:
    if not isinstance(request, dict):
        msg = "request must be a JSON object"
        raise TypeError(msg)
    oa_account = str(request.get("oa") or request.get("oa_account") or "").strip()
    if not oa_account:
        msg = "missing oa"
        raise ValueError(msg)
    return oa_account


def _lookup_with_reconnect(provider: DmStaffIdentityProvider, oa_account: str) -> Any:
    try:
        return provider.get_employee_context(oa_account)
    except Exception:
        provider.reset_connection()
        return provider.get_employee_context(oa_account)


def _write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
