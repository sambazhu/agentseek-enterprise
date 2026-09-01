from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from agentseek_wecom.addressing import long_connection_conversation_address
from agentseek_wecom.durable import SqliteDurableMessageStore
from pydantic import SecretStr

TEST_KEY_MATERIAL = "reconciliation-test-key-material-with-32-characters"


def _load_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "reconcile_wecom_inbox.py"
    spec = importlib.util.spec_from_file_location("enterprise_wecom_inbox_reconciliation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_lists_and_explicitly_requeues_one_failed_orphan(tmp_path, monkeypatch, capsys) -> None:
    database_path = tmp_path / "runtime" / "wecom.sqlite3"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join((
            "AGENTSEEK_WECOM_DURABLE_MODE=sqlite",
            "AGENTSEEK_WECOM_DURABLE_SQLITE_PATH=runtime/wecom.sqlite3",
            f"AGENTSEEK_WECOM_DURABLE_SECRET={TEST_KEY_MATERIAL}",
        )),
        encoding="utf-8",
    )
    store = SqliteDurableMessageStore(path=database_path, secret=SecretStr(TEST_KEY_MATERIAL))
    now = datetime.now(UTC)
    payload = {
        "msgid": "failed-orphan-message",
        "aibotid": "bot-1",
        "chattype": "group",
        "chatid": "group-epsilon",
        "from": {"userid": "user-1"},
        "msgtype": "text",
        "text": {"content": "需要人工恢复"},
    }
    address = long_connection_conversation_address(payload, tenant_id="tenant-1", interacted_at=now)
    inbox = store.admit_inbound(
        message_id="failed-orphan-message",
        address=address,
        stream_id="failed-orphan-stream",
        payload=payload,
        now=now,
    ).record
    claimed = store.claim_inbox(
        inbox.inbox_id,
        now=now,
        owner="dead-process",
        lease_duration=address.reply_deadline - now,
    )
    assert claimed is not None
    store.mark_inbox(inbox.inbox_id, "failed", now=now, error_type="NativeExit")
    monkeypatch.chdir(tmp_path)
    script = _load_script()

    assert script.main(["--env-file", str(env_path), "--list-failed"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["failed_without_outbox"] == [
        {
            "inbox_id": inbox.inbox_id,
            "stream_id": inbox.stream_id,
            "attempts": 1,
            "reply_deadline": address.reply_deadline.isoformat(),
            "last_error_type": "NativeExit",
            "eligibility": "requeueable",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    ]

    assert (
        script.main([
            "--env-file",
            str(env_path),
            "--requeue",
            inbox.inbox_id,
            "--gateway-stopped",
        ])
        == 0
    )
    requeued = json.loads(capsys.readouterr().out)
    assert requeued["status"] == "requeued"
    assert store.get_inbox(inbox.inbox_id).status == "pending"
