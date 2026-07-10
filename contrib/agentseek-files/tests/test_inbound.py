from __future__ import annotations

import asyncio

from agentseek_files.inbound import InboundFileResult, InboundFileService
from agentseek_files.models import ExtractResult, FileRecord, FileScope
from agentseek_files.plugin import CURRENT_FILES_CONTEXT_STATE_KEY, CURRENT_FILES_STATE_KEY, FilesPlugin
from agentseek_files.settings import FilesSettings


def test_mineru_defaults_enable_ocr_and_five_minute_polling() -> None:
    settings = FilesSettings()

    assert settings.mineru_is_ocr is True
    assert settings.mineru_poll_timeout_s == 300.0


def test_inbound_file_service_saves_extracts_and_builds_context(tmp_path):
    service = InboundFileService(
        FilesSettings(
            root_dir=tmp_path,
            allowed_extensions=(".txt",),
            extractor="local",
            extract_max_chars=100,
        )
    )
    scope = FileScope("hmac-t", "hmac-e", "hmac-s", channel="wecom", chat_id="hmac-c", message_id="hmac-m")

    result = asyncio.run(
        service.handle_bytes(
            scope=scope,
            filename="企微 报告.txt",
            data="第一行\n第二行".encode(),
            mime_type="text/plain",
        )
    )

    assert result.pending is False
    assert result.record.extract_status == "done"
    assert result.record.sanitized_filename == "file.txt"
    assert "第一行" in result.context_block
    assert "[CurrentFiles]" in result.context_block
    assert (tmp_path / result.record.relative_dir / "extracted.md").is_file()


def test_files_plugin_load_state_reads_channel_context(tmp_path):
    plugin = FilesPlugin(None, settings=FilesSettings(root_dir=tmp_path))
    context = {
        "files": {
            "current_files_context": "[CurrentFiles]\n- file_id: file_1",
            "records": [{"file_id": "file_1"}],
        }
    }

    state = asyncio.run(plugin.load_state({"content": "hi", "context": context}, "wecom:u1"))

    assert state[CURRENT_FILES_CONTEXT_STATE_KEY].startswith("[CurrentFiles]")
    assert state[CURRENT_FILES_STATE_KEY] == [{"file_id": "file_1"}]


def test_files_plugin_refreshes_completed_extract_on_next_turn(tmp_path, monkeypatch):
    settings = FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",), extractor="mineru")
    plugin = FilesPlugin(None, settings=settings)
    record = plugin.store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="report.pdf",
        data=b"%PDF-1.7\nmock",
        mime_type="application/pdf",
    )
    record = plugin.store.save_extract(
        record,
        ExtractResult(
            file_id=record.file_id,
            provider="mineru",
            status="pending",
            provider_task_id="batch-1",
            metadata={"mode": "extract_batch"},
        ),
    )

    async def keep_pending(current_record: FileRecord) -> InboundFileResult:
        return InboundFileResult(
            record=current_record,
            context_block="[CurrentFiles]\n  extract_status: pending\n[/CurrentFiles]",
            user_notice="pending",
            pending=True,
        )

    monkeypatch.setattr(plugin.inbound, "poll_pending", keep_pending)
    intake_context = {
        "files": {
            "current_files_context": "[CurrentFiles]\n  extract_status: pending\n[/CurrentFiles]",
            "records": [record.to_dict()],
        }
    }
    first_state = asyncio.run(
        plugin.load_state({"content": "上传文件", "context": intake_context}, "wecom:u1")
    )
    assert "extract_status: pending" in first_state[CURRENT_FILES_CONTEXT_STATE_KEY]

    plugin.store.save_extract(
        record,
        ExtractResult(
            file_id=record.file_id,
            provider="mineru",
            status="done",
            markdown="这是磁盘上刚完成的 PDF 提取结果",
            text="这是磁盘上刚完成的 PDF 提取结果",
            chars=18,
            provider_task_id="batch-1",
            metadata={"mode": "extract_batch", "state": "done"},
        ),
    )

    next_state = asyncio.run(plugin.load_state({"content": "文件内容是什么"}, "wecom:u1"))

    assert "extract_status: done" in next_state[CURRENT_FILES_CONTEXT_STATE_KEY]
    assert "这是磁盘上刚完成的 PDF 提取结果" in next_state[CURRENT_FILES_CONTEXT_STATE_KEY]
    assert next_state[CURRENT_FILES_STATE_KEY][0]["extract_status"] == "done"
