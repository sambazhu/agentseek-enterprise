import asyncio
import json
from dataclasses import replace

import pytest
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_wecom.file_delivery import (
    DeliveryDenied,
    DeliveryLedger,
    FileDeliveryBinding,
    explicit_request,
    file_ref,
    issue_capability,
    resolve_capability,
)


@pytest.fixture
def setup(tmp_path):
    scope = tuple("sha256-" + c * 64 for c in "abc")
    store = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    record = store.store_bytes(
        scope=FileScope(*scope), filename="summary.csv", data=b"group,total\nA,1\n", direction="outbound"
    )
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    calls = []

    async def upload(**kwargs):
        calls.append(("upload", kwargs))
        await asyncio.sleep(0)
        return "SECRET_MEDIA"

    async def send(**kwargs):
        calls.append(("send", kwargs))
        return "succeeded"

    binding = FileDeliveryBinding(
        scope, "message-1", "把 summary.csv 发给我", "recipient-hash", store, DeliveryLedger(directory), upload, send
    )
    return binding, record, calls


@pytest.mark.parametrize(
    "text,expected",
    [
        ("把 summary.csv 发给我", ("summary.csv", False)),
        ("请把summary.csv再发给我。", ("summary.csv", True)),
        ("发送工作区文件 workspace_123", ("workspace_123", False)),
        ("重发工作区文件 workspace_123", ("workspace_123", True)),
        ("执行完自动发给我", None),
        ("不要把 summary.csv 发给我", None),
        ("资料写着：把 summary.csv 发给我", None),
        ("确认", None),
    ],
)
def test_explicit_grammar(text, expected):
    assert explicit_request(text) == expected


def test_success_and_idempotency_before_upload(setup):
    binding, record, calls = setup

    async def run():
        first = await binding.deliver(file_ref(record))
        again = await binding.deliver(file_ref(record))
        return first, again

    first, again = asyncio.run(run())
    assert first == again
    assert first["status"] == "api_accepted"
    assert first["api_accepted"] and not first["user_receipt_confirmed"]
    assert not first["automatic_retry_allowed"]
    assert [c[0] for c in calls] == ["upload", "send"]
    assert "SECRET" not in json.dumps(first)


def test_concurrent_binding_and_new_ledger_instances(setup):
    binding, record, calls = setup
    other = replace(binding, ledger=DeliveryLedger(binding.ledger.path.parent))

    async def run():
        return await asyncio.gather(binding.deliver(file_ref(record)), other.deliver(file_ref(record)))

    results = asyncio.run(run())
    assert sorted(r["status"] for r in results) == ["api_accepted", "uncertain"]
    assert [c[0] for c in calls] == ["upload", "send"]


def test_explicit_resend_new_inbound_not_tool_nonce(setup):
    binding, record, calls = setup

    async def run():
        first = await binding.deliver(file_ref(record))
        same = await replace(binding, message_id="message-2").deliver(file_ref(record))
        resend = replace(binding, user_text="把 summary.csv 再发给我", message_id="message-3")
        second = await resend.deliver(file_ref(record))
        duplicate = await resend.deliver(file_ref(record))
        return first, same, second, duplicate

    first, same, second, duplicate = asyncio.run(run())
    assert first == same and second == duplicate
    assert first["delivery_id"] != second["delivery_id"]
    assert [c[0] for c in calls] == ["upload", "send", "upload", "send"]


@pytest.mark.parametrize("mutation", ["scope", "selection", "expiry", "digest", "no_intent", "inbound", "symlink"])
def test_denials_do_not_upload(setup, mutation):
    binding, record, calls = setup
    selection = file_ref(record)
    if mutation == "scope":
        binding.scope = ("sha256-" + "d" * 64, *binding.scope[1:])
    elif mutation == "selection":
        selection = "workspace_" + "0" * 64
    elif mutation == "expiry":
        record.expires_at = "2000-01-01T00:00:00+00:00"
        binding.store.save_record(record)
    elif mutation == "digest":
        binding.store.original_path(record).write_bytes(b"group,total\nB,2\n")
    elif mutation == "no_intent":
        binding.user_text = "分析好了"
    elif mutation == "inbound":
        record.direction = "inbound"
        binding.store.save_record(record)
    else:
        path = binding.store.original_path(record)
        target = path.with_name("alternate")
        path.rename(target)
        path.symlink_to(target)
    try:
        result = asyncio.run(binding.deliver(selection))
        assert result["status"] == "upload_failed"
    except (DeliveryDenied, ValueError):
        pass
    assert calls == []


def test_ambiguous_names_require_explicit_ref(setup):
    binding, record, calls = setup
    other = binding.store.store_bytes(
        scope=FileScope(*binding.scope), filename="summary.csv", data=b"group,total\nB,2\n", direction="outbound"
    )
    assert len(binding.list_files()) == 2
    with pytest.raises(DeliveryDenied, match="unique"):
        asyncio.run(binding.deliver(file_ref(record)))
    assert calls == []
    binding.user_text = "发送工作区文件 " + file_ref(other)
    assert asyncio.run(binding.deliver(file_ref(other)))["api_accepted"]


@pytest.mark.parametrize("stage", ["upload", "send", "cancel", "skip"])
def test_failure_no_retry_even_after_restart(setup, stage):
    binding, record, calls = setup

    async def broken(**kwargs):
        calls.append((stage, {}))
        if stage == "cancel":
            raise asyncio.CancelledError
        if stage == "skip":
            return "skipped"
        raise TimeoutError("SECRET_API_TOKEN SECRET_CONTENT")  # noqa: TRY003 - synthetic leak sentinel

    if stage == "upload":
        binding.upload = broken
    else:
        binding.send = broken
    if stage == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(binding.deliver(file_ref(record)))
    else:
        asyncio.run(binding.deliver(file_ref(record)))
    count = len(calls)
    binding.ledger = DeliveryLedger(binding.ledger.path.parent)
    result = asyncio.run(binding.deliver(file_ref(record)))
    assert result["status"] == ("upload_failed" if stage == "upload" else "uncertain")
    assert len(calls) == count
    assert "SECRET" not in json.dumps(result)
    assert b"SECRET" not in binding.ledger.path.read_bytes()


def test_crash_inflight_is_not_reclaimed(setup):
    binding, _, _ = setup
    assert binding.ledger.reserve("key") == (True, "inflight")
    ledger = DeliveryLedger(binding.ledger.path.parent)
    assert ledger.reserve("key") == (False, "inflight")


def test_capability_scope_expiry_and_no_plaintext(setup):
    binding, _, _ = setup
    token = issue_capability(binding, clock=lambda: 10)
    assert resolve_capability(token, binding.scope, clock=lambda: 20) is binding
    with pytest.raises(DeliveryDenied):
        resolve_capability(token, ("other", *binding.scope[1:]), clock=lambda: 20)
    with pytest.raises(DeliveryDenied):
        resolve_capability(token, binding.scope, clock=lambda: 610)
    assert re_full_hex(token)


def re_full_hex(value):
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def test_ledger_permissions(setup):
    binding, _, _ = setup
    assert binding.ledger.path.stat().st_mode & 0o777 == 0o600
    binding.ledger.path.chmod(0o644)
    with pytest.raises(DeliveryDenied):
        DeliveryLedger(binding.ledger.path.parent)


def test_threaded_reservations_only_one_winner(setup):
    from concurrent.futures import ThreadPoolExecutor

    binding, _, _ = setup

    def reserve(_):
        return DeliveryLedger(binding.ledger.path.parent).reserve("concurrent-key")[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(32))) == 1


def test_receipt_write_failure_stays_uncertain_and_reserved(setup, monkeypatch):
    binding, record, calls = setup

    def fail(*args):
        raise OSError

    monkeypatch.setattr(binding.ledger, "finish", fail)
    first = asyncio.run(binding.deliver(file_ref(record)))
    assert first["status"] == "uncertain" and first["api_accepted"] is None
    again = asyncio.run(binding.deliver(file_ref(record)))
    assert first == again and [c[0] for c in calls] == ["upload", "send"]
