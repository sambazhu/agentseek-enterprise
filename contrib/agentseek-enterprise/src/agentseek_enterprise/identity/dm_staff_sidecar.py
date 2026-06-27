from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from agentseek_enterprise.identity.dm_staff_provider import DmStaffIdentityProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DM employee identity subprocess sidecar.")
    parser.add_argument("--oa", required=True, help="OA account / plaintext WeCom userid.")
    args = parser.parse_args(argv)

    # The parent process enables subprocess mode. The child must perform the
    # actual JDBC lookup in-process, otherwise it would recursively spawn itself.
    os.environ["AGENTSEEK_IDENTITY_DM_EXECUTION_MODE"] = "in_process"

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


def _write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
