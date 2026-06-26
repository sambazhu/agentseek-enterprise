"""Send an encrypted local WeCom callback to the AgentSeek gateway."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agentseek_wecom.crypto import WeComJsonCrypto


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def post_encrypted(
    *,
    crypto: WeComJsonCrypto,
    url: str,
    payload: dict[str, Any],
    nonce: str,
) -> dict[str, Any]:
    timestamp = str(int(time.time()))
    encrypted = crypto.encrypt_message(json.dumps(payload, ensure_ascii=False), nonce=nonce, timestamp=timestamp)
    request_url = url + "?" + urllib.parse.urlencode(
        {
            "msg_signature": encrypted.msg_signature,
            "timestamp": timestamp,
            "nonce": nonce,
        }
    )
    request = urllib.request.Request(
        request_url,
        data=json.dumps({"encrypt": encrypted.encrypt}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    response_body = json.loads(body)
    plain = crypto.decrypt_message(
        post_data=json.dumps({"encrypt": response_body["encrypt"]}, ensure_ascii=False),
        msg_signature=response_body["msgsignature"],
        timestamp=response_body["timestamp"],
        nonce=response_body["nonce"],
    )
    return {"status": status, "plain": json.loads(plain)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--oa", default="chenkang2")
    parser.add_argument("--text", default="本地企微链路联调")
    parser.add_argument("--polls", type=int, default=3)
    args = parser.parse_args()

    load_dotenv(Path(args.env_file))
    token = require_env("AGENTSEEK_WECOM_TOKEN")
    aes_key = require_env("AGENTSEEK_WECOM_ENCODING_AES_KEY")
    port = int(os.environ.get("AGENTSEEK_WECOM_PORT", "12000"))
    path = require_env("AGENTSEEK_WECOM_CALLBACK_PATH")
    receive_id = os.environ.get("AGENTSEEK_WECOM_RECEIVE_ID", "")
    url = f"http://{args.host}:{port}{path}"
    crypto = WeComJsonCrypto(token=token, encoding_aes_key=aes_key, receive_id=receive_id)

    first = post_encrypted(
        crypto=crypto,
        url=url,
        nonce="agentseek-local-probe",
        payload={
            "msgtype": "text",
            "from": {"userid": args.oa},
            "text": {"content": args.text},
        },
    )
    stream = first["plain"].get("stream") if isinstance(first["plain"], dict) else None
    result = {"callback_status": first["status"], "first_response": first["plain"], "poll_responses": []}
    if isinstance(stream, dict) and stream.get("id") and not stream.get("finish"):
        for index in range(max(args.polls, 0)):
            time.sleep(0.5)
            polled = post_encrypted(
                crypto=crypto,
                url=url,
                nonce=f"agentseek-local-poll-{index}",
                payload={"msgtype": "stream", "stream": {"id": stream["id"]}},
            )
            result["poll_responses"].append(polled["plain"])
            polled_stream = polled["plain"].get("stream") if isinstance(polled["plain"], dict) else None
            if isinstance(polled_stream, dict) and polled_stream.get("finish"):
                break

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
