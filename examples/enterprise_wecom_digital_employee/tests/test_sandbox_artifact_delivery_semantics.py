"""Characterize existing same-content semantics; no live execution or sends."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import BusinessStore
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_wecom.file_delivery import DeliveryDenied, DeliveryLedger, FileDeliveryBinding, file_ref


@pytest.fixture
def case(tmp_path):
    scope = tuple("sha256-" + c * 64 for c in "abc")
    now = datetime(2030, 1, 1, 10, tzinfo=UTC)
    data = b"group,total\nA,1\n"
    files = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    private = tmp_path / "ledger"
    private.mkdir(mode=0o700)
    ledger, calls = DeliveryLedger(private), []

    async def upload(**kwargs):
        calls.append("upload")
        return "synthetic-media"

    async def send(**kwargs):
        calls.append("send")
        return "succeeded"

    def save(at=now, content=data):
        return files.store_bytes(scope=FileScope(*scope), filename="summary.csv", data=content,
                                 direction="outbound", now=at)

    binding = FileDeliveryBinding(scope, "message-1", "把 summary.csv 发给我", "recipient",
        files, ledger, upload, send, clock=lambda: now.timestamp())
    return save, binding, calls, now


def test_same_day_same_content_reuses_ref_and_rewrites_times(case):
    save, binding, calls, now = case
    first = save()
    second = save(now + timedelta(hours=1))
    assert first.relative_dir == second.relative_dir and file_ref(first) == file_ref(second)
    assert first.created_at != second.created_at and first.expires_at != second.expires_at
    assert binding.store.load_record(first.relative_dir).to_dict() == second.to_dict()
    assert len(binding.list_files()) == 1 and calls == []


def test_two_attempts_same_bytes_are_distinct_but_workspace_is_shared(case, tmp_path):
    save, binding, calls, now = case
    directory = tmp_path / "business"
    directory.mkdir(mode=0o700)
    store = BusinessStore(directory)
    outputs = []
    for number in (1, 2):
        request = BusinessRequest(f"request-{number}", "same-owner", "file-input", "sum")
        attempt = store.reserve(request)
        artifact = store.persist(attempt, request.owner_id, b"group,total\nA,1\n")
        store.record(attempt, "succeeded", artifact)
        record = save(now + timedelta(minutes=number))
        outputs.append((attempt, artifact, file_ref(record)))
    assert outputs[0][0] != outputs[1][0] and outputs[0][1] != outputs[1][1]
    assert outputs[0][2] == outputs[1][2]
    assert len(binding.list_files()) == 1 and calls == []


def test_second_task_same_file_initial_delivery_returns_old_receipt(case):
    save, binding, calls, now = case

    async def run():
        first = await binding.deliver(file_ref(save()))
        record = save(now + timedelta(minutes=1))
        second = await replace(binding, message_id="message-2").deliver(file_ref(record))
        assert first == second and first["status"] == "api_accepted"
        assert calls == ["upload", "send"]
        resend = replace(binding, message_id="message-3", user_text="把 summary.csv 再发给我")
        third = await resend.deliver(file_ref(record))
        assert third["delivery_id"] != first["delivery_id"]
        assert await resend.deliver(file_ref(record)) == third
        assert calls == ["upload", "send", "upload", "send"]

    asyncio.run(run())


@pytest.mark.parametrize("different_day", [False, True])
def test_other_content_or_day_creates_ambiguous_filename(case, different_day):
    save, binding, calls, now = case
    first = save()
    second = save(now + timedelta(days=1)) if different_day else save(content=b"group,total\nB,2\n")
    assert file_ref(first) != file_ref(second)
    assert len(binding.list_files()) == 2
    with pytest.raises(DeliveryDenied, match="unique"):
        asyncio.run(binding.deliver(file_ref(second)))
    assert calls == []
    exact = replace(binding, user_text="发送工作区文件 " + file_ref(second))
    assert asyncio.run(exact.deliver(file_ref(second)))["api_accepted"]
