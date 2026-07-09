from __future__ import annotations

import asyncio

from agentseek_files.inbound import InboundFileService
from agentseek_files.models import FileScope
from agentseek_files.plugin import CURRENT_FILES_CONTEXT_STATE_KEY, CURRENT_FILES_STATE_KEY, FilesPlugin
from agentseek_files.settings import FilesSettings


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
    plugin = FilesPlugin(None)
    plugin.settings = FilesSettings(root_dir=tmp_path)
    context = {
        "files": {
            "current_files_context": "[CurrentFiles]\n- file_id: file_1",
            "records": [{"file_id": "file_1"}],
        }
    }

    state = plugin.load_state({"content": "hi", "context": context}, "wecom:u1")

    assert state[CURRENT_FILES_CONTEXT_STATE_KEY].startswith("[CurrentFiles]")
    assert state[CURRENT_FILES_STATE_KEY] == [{"file_id": "file_1"}]
