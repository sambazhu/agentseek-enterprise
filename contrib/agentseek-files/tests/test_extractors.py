from __future__ import annotations

import asyncio

from agentseek_files.context import build_current_files_context
from agentseek_files.extractors.local import LocalTextExtractor
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore


def test_local_text_extractor_truncates_and_builds_context(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".md",)))
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="note.md",
        data="第一行\n第二行\n第三行".encode(),
        mime_type="text/markdown",
    )
    extractor = LocalTextExtractor(max_chars=5)

    result = asyncio.run(extractor.extract(record, store.original_path(record)))
    updated = store.save_extract(record, result)

    assert result.status == "done"
    assert result.chars == 5
    assert updated.extract_status == "done"
    context = build_current_files_context([updated], {updated.file_id: result.markdown})
    assert "[CurrentFiles]" in context
    assert "note.md" in context
    assert str(tmp_path) not in context
