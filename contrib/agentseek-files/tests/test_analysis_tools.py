from __future__ import annotations

from datetime import UTC, datetime

from agentseek_files.analysis_tools import _analyze_current_file, file_analysis_tools
from agentseek_files.models import ExtractResult, FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore


def test_analyze_file_groups_complete_extracted_table_by_class(tmp_path) -> None:
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".xlsx",)))
    record = store.store_bytes(
        scope=FileScope("tenant", "employee", "session"),
        filename="退餐统计.xlsx",
        data=b"original-binary-must-not-be-read",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )
    markdown = (
        "<table><tr><th>序号</th><th>班级</th><th>姓名</th><th>金额</th></tr>"
        "<tr><td>1</td><td>一一班</td><td>张三</td><td>10</td></tr>"
        "<tr><td>2</td><td>一一班</td><td>李四</td><td>20</td></tr>"
        "<tr><td>3</td><td>一二班</td><td>王五</td><td>30</td></tr>"
        "<tr><td>4</td><td>一二班</td><td>王五</td><td>30</td></tr>"
        "</table>"
    )
    store.save_extract(
        record,
        ExtractResult(
            file_id=record.file_id,
            provider="test",
            status="done",
            markdown=markdown,
            chars=len(markdown),
        ),
    )

    result = _analyze_current_file(
        store,
        {"current_files": [record.to_dict()]},
        file_id=record.file_id,
        question="每班有多少个人退餐？",
    )

    assert "表格数据行数（不含表头）: 4" in result
    assert "唯一人员数（按姓名去重）: 3" in result
    assert "按“班级”统计:" in result
    assert "- 一一班: 2" in result
    assert "- 一二班: 1" in result
    assert "original-binary-must-not-be-read" not in result
    assert "基于完整 extracted.md/extracted.txt" in result


def test_analyze_file_rejects_file_outside_current_state(tmp_path) -> None:
    store = LocalFileStore(FilesSettings(root_dir=tmp_path))

    result = _analyze_current_file(store, {"current_files": []}, file_id="file_other", question="统计")

    assert "不在当前会话" in result


def test_file_analysis_tools_exposes_analyze_file(tmp_path) -> None:
    store = LocalFileStore(FilesSettings(root_dir=tmp_path))

    tools = file_analysis_tools(store)

    assert [tool.name for tool in tools] == ["analyze_file"]
    schema = tools[0].tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"file_id", "question"}
