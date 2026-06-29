"""Probe Enterprise WeChat userid resolution for intelligent robot callbacks."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {"access_token", "corpsecret", "secret"}


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_base_url() -> str:
    return os.environ.get("AGENTSEEK_WECOM_API_BASE_URL", "https://qyapi.weixin.qq.com").strip().rstrip("/")


def get_json(url: str, *, timeout: int = 15, label: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    return _open_json(request, timeout=timeout, label=label)


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 15, label: str = "POST") -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    return _open_json(request, timeout=timeout, label=label)


def _open_json(request: urllib.request.Request, *, timeout: int, label: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{label} failed with HTTP {exc.code}: {raw[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{label} network error: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} returned non-JSON response: {raw[:500]}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Unexpected JSON response: {data!r}")
    return data


def get_access_token() -> str:
    corp_id = require_env("AGENTSEEK_WECOM_CORP_ID")
    app_secret = require_env("AGENTSEEK_WECOM_APP_SECRET")
    query = urllib.parse.urlencode({"corpid": corp_id, "corpsecret": app_secret})
    data = get_json(f"{api_base_url()}/cgi-bin/gettoken?{query}", label="gettoken")
    if data.get("errcode") != 0:
        raise SystemExit(f"gettoken failed: {redact(data)}")
    token = str(data.get("access_token") or "")
    if not token:
        raise SystemExit(f"gettoken response missing access_token: {redact(data)}")
    return token


def convert_open_userid(access_token: str, open_userid: str) -> str:
    query = urllib.parse.urlencode({"access_token": access_token})
    data = post_json(
        f"{api_base_url()}/cgi-bin/batch/openuserid_to_userid?{query}",
        {"open_userid_list": [open_userid]},
        label="openuserid_to_userid",
    )
    if data.get("errcode") != 0:
        raise SystemExit(f"openuserid_to_userid failed: {redact(data)}")
    invalid = data.get("invalid_open_userid_list") or []
    if open_userid in invalid:
        raise SystemExit(f"open userid is invalid or not visible to the app: {open_userid}")
    for item in data.get("userid_list") or []:
        if isinstance(item, dict) and item.get("open_userid") == open_userid and item.get("userid"):
            return str(item["userid"])
    raise SystemExit(f"openuserid_to_userid response missing userid: {redact(data)}")


def get_member(access_token: str, userid: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"access_token": access_token, "userid": userid})
    data = get_json(f"{api_base_url()}/cgi-bin/user/get?{query}", label="user/get")
    if data.get("errcode") != 0:
        raise SystemExit(f"user/get failed: {redact(data)}")
    return data


def extract_account(member: dict[str, Any]) -> dict[str, Any]:
    ext_attrs: dict[str, str] = {}
    raw_attrs = ((member.get("extattr") or {}).get("attrs") or []) if isinstance(member.get("extattr"), dict) else []
    for item in raw_attrs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        text = item.get("text") if isinstance(item.get("text"), dict) else {}
        value = str(text.get("value") or "").strip()
        if name and value:
            ext_attrs[name] = value

    candidates = {
        "alias": member.get("alias"),
        "email": member.get("email"),
        "biz_mail": member.get("biz_mail"),
        "mobile": member.get("mobile"),
    }
    for key, value in ext_attrs.items():
        candidates[f"extattr.{key}"] = value

    return {
        "userid": member.get("userid"),
        "name": member.get("name"),
        "department": member.get("department"),
        "candidate_accounts": {key: value for key, value in candidates.items() if value},
    }


def redact(value: object) -> object:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("open_userid", help="Encrypted open_userid from the intelligent robot callback.")
    parser.add_argument("--base-url", help="Override AGENTSEEK_WECOM_API_BASE_URL for this probe.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--skip-user-get", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(args.env_file))
    if args.base_url:
        os.environ["AGENTSEEK_WECOM_API_BASE_URL"] = args.base_url
    access_token = get_access_token()
    userid = convert_open_userid(access_token, args.open_userid)
    result: dict[str, Any] = {"ok": True, "open_userid": args.open_userid, "userid": userid}
    if not args.skip_user_get:
        member = get_member(access_token, userid)
        result["member_summary"] = extract_account(member)
    json.dump(redact(result), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
