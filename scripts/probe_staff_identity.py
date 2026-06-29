#!/usr/bin/env python3
"""Probe employee identity providers.

Default mode probes the Python runtime DB provider.

Java API mode probes the newer ai-bot endpoint:
  POST {YWZT_API_URL}/robot-platform-service/api/staff/queryStaffInfo

Legacy mode probes qywx-ai-bot-1.1:
  POST {YWZT_API_URL}/scrm-ai-service/api/staff/getStaffInfoWithRole
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTERPRISE_SRC = ROOT / "contrib" / "agentseek-enterprise" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ENTERPRISE_SRC) not in sys.path:
    sys.path.insert(0, str(ENTERPRISE_SRC))

from agentseek_enterprise.identity import DmStaffIdentityProvider  # noqa: E402

ENV_FILES = (
    ROOT / ".env",
    ROOT / "ai-bot" / "qywx-ai-bot" / ".env",
    ROOT / "qywx-ai-bot-1.1" / ".env",
)

NEW_STAFF_PATH = "/robot-platform-service/api/staff/queryStaffInfo"
LEGACY_STAFF_PATH = "/scrm-ai-service/api/staff/getStaffInfoWithRole"
DEFAULT_NEW_BASE_URL = "https://robot.wkzq.com.cn/ai-bot"

BELONG_TO_LABELS = {
    1: "公司总部",
    2: "金通子公司",
    3: "金鼎子公司",
    4: "分支机构",
}

ROLE_LABELS = {
    1: "总部员工",
    2: "分支机构负责人",
    3: "营业部普通员工",
}


def _load_dotenv_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if parsed else ""
        except ValueError:
            os.environ[key] = value.strip().strip("'\"")


def _load_env(extra_env_files: list[str]) -> None:
    for path in ENV_FILES:
        _load_dotenv_file(path)
    for filename in extra_env_files:
        _load_dotenv_file(Path(filename).expanduser().resolve())


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing required env var: {name}", file=sys.stderr)
        print("Set it in .env, ai-bot/qywx-ai-bot/.env, or pass --base-url.", file=sys.stderr)
        raise SystemExit(2)
    return value


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _b64url(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(data).decode("ascii")
    return encoded.rstrip("=")


def _generate_jwt() -> str:
    secret = _require_env("YWZT_JWT_SECRET")
    issuer = os.environ.get("YWZT_JWT_ISSUER", "wecom-oa-robot")
    expire_seconds = int(os.environ.get("YWZT_JWT_EXPIRE_SECONDS", "7200"))
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": "qywx-ai-bot",
        "iss": issuer,
        "iat": now,
        "exp": now + expire_seconds,
    }
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    ).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} from identity API:", file=sys.stderr)
        print(error_body, file=sys.stderr)
        raise SystemExit(1) from exc
    except urllib.error.URLError as exc:
        print(f"Could not reach identity API: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError as exc:
        print("Identity API did not return JSON:", file=sys.stderr)
        print(response_body, file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(data, dict):
        print(f"Identity API returned non-object JSON: {type(data)!r}", file=sys.stderr)
        raise SystemExit(1)
    return data


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _print_selected_fields(record: dict[str, Any], keys: list[str]) -> None:
    print("- selected fields:")
    for key in keys:
        if key in record:
            print(f"  {key}: {json.dumps(record[key], ensure_ascii=False, default=str)}")


def _summarize_new(data: dict[str, Any]) -> None:
    print("\nSummary")
    print(f"- err_code: {data.get('err_code')!r}")
    print(f"- err_msg: {data.get('err_msg')!r}")

    records = data.get("data")
    if not isinstance(records, list) or not records:
        print("- data: empty or not a list")
        return

    record = records[0]
    if not isinstance(record, dict):
        print(f"- first record: {type(record)!r}")
        return

    print("- first record keys:")
    for key in sorted(record):
        print(f"  - {key}")

    _print_selected_fields(
        record,
        [
            "id",
            "ygbh",
            "fdName",
            "fdLoginName",
            "fdSex",
            "deptName",
            "deptId",
            "parentId",
            "belongTo",
            "role",
            "ladpDn",
            "hierarchyId",
            "travelerType",
            "travelerIdentity",
            "travelerInvestment",
            "travelerDelegate",
            "org_name",
            "yyb_id",
            "org_permission",
        ],
    )

    belong_to = _as_int(record.get("belongTo"))
    role = _as_int(record.get("role"))
    dept_id = record.get("deptId") or record.get("parentId")
    print("- EmployeeContext draft:")
    print(f"  user_id: {json.dumps(record.get('id'), ensure_ascii=False, default=str)}")
    print(f"  employee_no: {json.dumps(record.get('ygbh'), ensure_ascii=False, default=str)}")
    print(f"  oa_account: {json.dumps(record.get('fdLoginName'), ensure_ascii=False, default=str)}")
    print(f"  name: {json.dumps(record.get('fdName'), ensure_ascii=False, default=str)}")
    print(f"  org_subject: {BELONG_TO_LABELS.get(belong_to, belong_to)!r}")
    print(f"  role: {ROLE_LABELS.get(role, role)!r}")
    print(f"  dept: {json.dumps(record.get('deptName'), ensure_ascii=False, default=str)}")
    print(f"  dept_id: {json.dumps(dept_id, ensure_ascii=False, default=str)}")
    print(f"  post: {json.dumps(record.get('post'), ensure_ascii=False, default=str)}")


def _summarize_legacy(data: dict[str, Any]) -> None:
    print("\nSummary")
    print(f"- err_code: {data.get('err_code')!r}")
    print(f"- err_msg: {data.get('err_msg')!r}")

    records = data.get("data")
    if not isinstance(records, list) or not records:
        print("- data: empty or not a list")
        return

    record = records[0]
    if not isinstance(record, dict):
        print(f"- first record: {type(record)!r}")
        return

    print("- first record keys:")
    for key in sorted(record):
        print(f"  - {key}")

    _print_selected_fields(
        record,
        [
            "ygbh",
            "ygxm",
            "adzh",
            "org_name",
            "yyb_id",
            "role",
            "org_type",
            "org_type_name",
            "company_type",
            "department",
            "dept_name",
            "subsidiary",
            "branch",
            "org_permission",
        ],
    )


def _employee_context_payload(oa_account: str) -> dict[str, Any]:
    try:
        context = DmStaffIdentityProvider().get_employee_context(oa_account)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if context is None:
        return {
            "err_code": 404,
            "err_msg": "未找到员工信息",
            "data": None,
        }
    return {
        "err_code": 0,
        "err_msg": "success",
        "data": [context.to_java_api_record()],
        "employee_context": context.to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe staff identity API response shape.")
    parser.add_argument("--oa", help="OA account or WeCom userid.")
    parser.add_argument("--staff-no", help="Employee number.")
    parser.add_argument("--raw", action="store_true", help="Print the full raw JSON response.")
    parser.add_argument(
        "--source",
        choices=["python-db", "java-api", "legacy-api"],
        default="python-db",
        help="Identity source to probe.",
    )
    parser.add_argument("--legacy", action="store_true", help="Shortcut for --source legacy-api.")
    parser.add_argument("--base-url", help="Override YWZT_API_URL.")
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Load an additional dotenv file after the defaults.",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    args = parser.parse_args()

    if args.legacy:
        args.source = "legacy-api"

    if not args.oa and not args.staff_no:
        parser.error("Pass --oa <account> or --staff-no <employee_no>.")
    if args.source == "python-db" and not args.oa:
        parser.error("--source python-db requires --oa <account>.")

    _load_env(args.env_file)

    if args.source == "python-db":
        print("Mode: python-db")
        print(f"Payload: {json.dumps({'fdLoginName': args.oa}, ensure_ascii=False)}")
        data = _employee_context_payload(args.oa)
        _summarize_new(data)
    elif args.source == "legacy-api":
        base_url = (args.base_url or _require_env("YWZT_API_URL")).rstrip("/")
        token = _require_env("YWZT_API_TOKEN")
        url = _join_url(base_url, LEGACY_STAFF_PATH)
        payload = {"ygbh": args.staff_no, "adzh": args.oa}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        auth_label = f"legacy bearer {_mask(token)}"
        summarize = _summarize_legacy
        print("Mode: legacy-api")
        print(f"Calling: {url}")
        print(f"Authorization: {auth_label}")
        print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
        data = _post_json(url, headers, payload, args.timeout)
        summarize(data)
    else:
        base_url = (args.base_url or os.environ.get("YWZT_API_URL") or DEFAULT_NEW_BASE_URL).rstrip("/")
        token = _generate_jwt()
        url = _join_url(base_url, NEW_STAFF_PATH)
        payload = {"ygbh": args.staff_no, "fdLoginName": args.oa}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        auth_label = f"jwt bearer {_mask(token)}"
        summarize = _summarize_new
        print("Mode: java-api")
        print(f"Calling: {url}")
        print(f"Authorization: {auth_label}")
        print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
        data = _post_json(url, headers, payload, args.timeout)
        summarize(data)

    if args.raw:
        print("\nRaw JSON")
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
