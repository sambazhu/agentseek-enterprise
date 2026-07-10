from __future__ import annotations

import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

import pytest
from agentseek_enterprise import long_term_memory
from agentseek_enterprise.langgraph_store import SQLiteStore
from agentseek_enterprise.long_term_memory import _PROFILE_PATH, employee_memory_tools
from agentseek_enterprise.runtime import (
    EnterpriseRuntimeSettings,
    enterprise_filesystem_namespace,
    enterprise_runtime_context,
)
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime

TestToolRuntime = ToolRuntime[Mapping[str, object], dict[str, Any]]

DIRTY_ZHUCHUNLIN_PROFILE = """# Employee Memory
- [work_context] 朱春霖明天（2026-07-01）下午去北京出差
- [preference] 企微回复偏好简洁、分点的回复方式
- [work_context] 明天（2026/7/1）下午去深圳出差
- [preference] 企微回复偏好：简洁、分点呈现
- [work_context] 明天（2026-07-01）下午去深圳出差
- [preference] 偏好简洁、分点的回复方式
- [work_context] 2026年7月2日下午去深圳出差
- [work_context] 负责数据架构工作
"""


def test_remember_exact_duplicate_still_keeps_one_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    first = tools["remember_employee_memory"].func(
        memory="Prefer concise WeCom replies.",
        category="preference",
        runtime=runtime,
    )
    second = tools["remember_employee_memory"].func(
        memory="Prefer concise WeCom replies.",
        category="preference",
        runtime=runtime,
    )

    assert "recorded" in first
    assert "already recorded" in second
    assert _memory_line_count(_profile_content(store, runtime), "preference") == 1


def test_remember_near_duplicate_preferences_updates_latest_wording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    memories = [
        "企微回复偏好简洁、分点的回复方式",
        "企微回复偏好：简洁、分点呈现",
        "偏好简洁、分点的回复方式",
    ]
    results = [
        tools["remember_employee_memory"].func(memory=memory, category="preference", runtime=runtime)
        for memory in memories
    ]

    profile = _profile_content(store, runtime)
    assert "recorded" in results[0]
    assert results[1:] == [
        "Updated an existing durable memory (near-duplicate).",
        "Updated an existing durable memory (near-duplicate).",
    ]
    assert _memory_line_count(profile, "preference") == 1
    assert "- [preference] 偏好简洁、分点的回复方式" in profile
    assert "企微回复偏好简洁、分点的回复方式" not in profile


def test_remember_never_dedupes_across_categories(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="偏好简洁、分点的回复方式",
        category="preference",
        runtime=runtime,
    )
    tools["remember_employee_memory"].func(
        memory="偏好简洁、分点的回复方式",
        category="work_context",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert _memory_line_count(profile, "preference") == 1
    assert _memory_line_count(profile, "work_context") == 1


def test_slot_conflict_supersedes_with_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    first = tools["remember_employee_memory"].func(
        memory="明天去北京出差",
        category="work_context",
        slot="travel_plan",
        runtime=runtime,
    )
    second = tools["remember_employee_memory"].func(
        memory="明天去深圳出差",
        category="work_context",
        slot="travel_plan",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert "recorded" in first
    assert second == "已更新『出差计划』: 之前记的是「明天去北京出差」, 现在改为「明天去深圳出差」。"
    assert _memory_lines(profile, "work_context") == ["- [work_context|slot=travel_plan] 明天去深圳出差"]
    assert "北京" not in profile


def test_slot_near_duplicate_uses_silent_p0_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="企微回复偏好简洁、分点的回复方式",
        category="preference",
        slot="reply_style",
        runtime=runtime,
    )
    result = tools["remember_employee_memory"].func(
        memory="企微回复偏好：简洁、分点呈现",
        category="preference",
        slot="reply_style",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert result == "Updated an existing durable memory (near-duplicate)."
    assert _memory_lines(profile, "preference") == ["- [preference|slot=reply_style] 企微回复偏好：简洁、分点呈现"]


def test_different_slots_keep_separate_memories(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="明天去深圳出差",
        category="work_context",
        slot="travel_plan",
        runtime=runtime,
    )
    tools["remember_employee_memory"].func(
        memory="明天参加数据治理评审会",
        category="work_context",
        slot="meeting_plan",
        runtime=runtime,
    )

    assert _memory_lines(_profile_content(store, runtime), "work_context") == [
        "- [work_context|slot=travel_plan] 明天去深圳出差",
        "- [work_context|slot=meeting_plan] 明天参加数据治理评审会",
    ]


def test_bare_responsibility_slot_keeps_distinct_duties(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    first = tools["remember_employee_memory"].func(
        memory="负责数据架构工作",
        category="work_context",
        slot="responsibility",
        runtime=runtime,
    )
    second = tools["remember_employee_memory"].func(
        memory="负责 AI 架构工作",
        category="work_context",
        slot="responsibility",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert "recorded" in first
    assert "recorded" in second
    assert _memory_lines(profile, "work_context") == [
        "- [work_context|slot=responsibility] 负责数据架构工作",
        "- [work_context|slot=responsibility] 负责 AI 架构工作",
    ]


def test_scoped_responsibility_slots_keep_distinct_duties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="负责数据架构工作",
        category="work_context",
        slot="responsibility.data_arch",
        runtime=runtime,
    )
    tools["remember_employee_memory"].func(
        memory="负责 AI 架构工作",
        category="work_context",
        slot="responsibility.ai_arch",
        runtime=runtime,
    )

    assert _memory_lines(_profile_content(store, runtime), "work_context") == [
        "- [work_context|slot=responsibility.data_arch] 负责数据架构工作",
        "- [work_context|slot=responsibility.ai_arch] 负责 AI 架构工作",
    ]


def test_same_slot_different_categories_do_not_supersede(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="偏好简洁、分点的回复方式",
        category="preference",
        slot="reply_style",
        runtime=runtime,
    )
    tools["remember_employee_memory"].func(
        memory="偏好简洁、分点的回复方式",
        category="work_context",
        slot="reply_style",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert _memory_lines(profile, "preference") == ["- [preference|slot=reply_style] 偏好简洁、分点的回复方式"]
    assert _memory_lines(profile, "work_context") == ["- [work_context|slot=reply_style] 偏好简洁、分点的回复方式"]


def test_dedup_threshold_one_keeps_exact_match_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", "1.0")
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="企微回复偏好简洁、分点的回复方式",
        category="preference",
        runtime=runtime,
    )
    near_duplicate = tools["remember_employee_memory"].func(
        memory="企微回复偏好：简洁、分点呈现",
        category="preference",
        runtime=runtime,
    )
    exact_duplicate = tools["remember_employee_memory"].func(
        memory="企微回复偏好：简洁、分点呈现",
        category="preference",
        runtime=runtime,
    )

    assert "recorded" in near_duplicate
    assert "already recorded" in exact_duplicate
    assert _memory_line_count(_profile_content(store, runtime), "preference") == 2


def test_dedup_threshold_zero_disables_near_duplicate_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", "0.0")
    store, runtime, tools = _memory_harness(tmp_path)

    for memory in (
        "企微回复偏好简洁、分点的回复方式",
        "企微回复偏好：简洁、分点呈现",
        "偏好简洁、分点的回复方式",
    ):
        tools["remember_employee_memory"].func(memory=memory, category="preference", runtime=runtime)

    assert _memory_line_count(_profile_content(store, runtime), "preference") == 3


def test_slot_supersession_env_false_uses_unslotted_p0_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", "false")
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)

    tools["remember_employee_memory"].func(
        memory="明天去北京出差",
        category="work_context",
        slot="travel_plan",
        runtime=runtime,
    )
    result = tools["remember_employee_memory"].func(
        memory="明天去深圳出差",
        category="work_context",
        slot="travel_plan",
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert "recorded" in result
    assert _memory_lines(profile, "work_context") == [
        "- [work_context] 明天去北京出差",
        "- [work_context] 明天去深圳出差",
    ]


def test_slot_supersession_env_false_recall_treats_slots_as_plain_p0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", "false")
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(
        store,
        runtime,
        "# Employee Memory\n"
        "- [preference|slot=reply_style] 企微回复偏好简洁、分点的回复方式\n"
        "- [preference|slot=other_reply_style] 偏好简洁、分点的回复方式\n",
    )

    recalled = tools["recall_employee_memory"].func(runtime=runtime)

    assert _memory_lines(recalled, "preference") == ["- [preference] 偏好简洁、分点的回复方式"]


def test_sensitive_and_size_refusals_are_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    _store, runtime, tools = _memory_harness(tmp_path)

    sensitive = tools["remember_employee_memory"].func(
        memory="api key is private",
        category="preference",
        runtime=runtime,
    )

    assert "Refused" in sensitive
    with pytest.raises(ValueError, match="at most"):
        tools["remember_employee_memory"].func(
            memory="x" * 501,
            category="preference",
            runtime=runtime,
        )


def test_recall_dedupes_dirty_profile_as_read_only_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(store, runtime, DIRTY_ZHUCHUNLIN_PROFILE)

    recalled = tools["recall_employee_memory"].func(runtime=runtime)

    assert _profile_content(store, runtime) == DIRTY_ZHUCHUNLIN_PROFILE
    preference_lines = _memory_lines(recalled, "preference")
    assert preference_lines == ["- [preference] 偏好简洁、分点的回复方式"]
    assert "朱春霖明天（2026-07-01）下午去北京出差" in recalled
    assert "明天（2026-07-01）下午去深圳出差" in recalled
    assert "[DurableEmployeeMemory]" in recalled
    assert "do not mix unrelated short-term" in recalled


def test_recall_threshold_zero_disables_dirty_profile_view_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", "0.0")
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(store, runtime, DIRTY_ZHUCHUNLIN_PROFILE)

    recalled = tools["recall_employee_memory"].func(runtime=runtime)

    assert len(_memory_lines(recalled, "preference")) == 3
    assert _profile_content(store, runtime) == DIRTY_ZHUCHUNLIN_PROFILE


def test_recall_keeps_legacy_and_slotted_profile_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(
        store,
        runtime,
        "# Employee Memory\n"
        "- [work_context] 负责数据架构工作\n"
        "- [preference|slot=reply_style] 偏好简洁、分点的回复方式\n",
    )

    recalled = tools["recall_employee_memory"].func(runtime=runtime)

    assert "- [work_context] 负责数据架构工作" in recalled
    assert "- [preference|slot=reply_style] 偏好简洁、分点的回复方式" in recalled


def test_forget_employee_memory_still_removes_matching_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    tools["remember_employee_memory"].func(
        memory="偏好简洁、分点的回复方式",
        category="preference",
        runtime=runtime,
    )
    tools["remember_employee_memory"].func(
        memory="负责数据架构工作",
        category="work_context",
        runtime=runtime,
    )
    _set_latest_user_message(runtime, "请忘记偏好简洁、分点的回复方式")

    result = tools["forget_employee_memory"].func(memory="偏好简洁、分点的回复方式", runtime=runtime)

    profile = _profile_content(store, runtime)
    assert "removed" in result
    assert "偏好简洁、分点的回复方式" not in profile
    assert "负责数据架构工作" in profile


def test_forget_refuses_without_explicit_latest_user_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    for memory in ("负责数据架构工作", "负责 AI 架构工作"):
        tools["remember_employee_memory"].func(
            memory=memory,
            category="work_context",
            slot="responsibility",
            runtime=runtime,
        )
    _set_latest_user_message(runtime, "我有几条工作职责？")

    result = tools["forget_employee_memory"].func(memory="负责数据架构工作", runtime=runtime)

    profile = _profile_content(store, runtime)
    assert result.startswith("Refused:")
    assert "负责数据架构工作" in profile
    assert "负责 AI 架构工作" in profile


def test_forget_refuses_negated_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    store, runtime, tools = _memory_harness(tmp_path)
    tools["remember_employee_memory"].func(
        memory="负责数据架构工作",
        category="work_context",
        runtime=runtime,
    )
    _set_latest_user_message(runtime, "不要忘记我负责数据架构工作")

    result = tools["forget_employee_memory"].func(memory="负责数据架构工作", runtime=runtime)

    assert result.startswith("Refused:")
    assert "负责数据架构工作" in _profile_content(store, runtime)


def test_forget_requires_exact_memory_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    store, runtime, tools = _memory_harness(tmp_path)
    tools["remember_employee_memory"].func(
        memory="负责数据架构工作",
        category="work_context",
        runtime=runtime,
    )
    _set_latest_user_message(runtime, "请删除数据架构这条长期记忆")

    result = tools["forget_employee_memory"].func(memory="数据架构", runtime=runtime)

    assert result == "No matching durable employee memory was found."
    assert "负责数据架构工作" in _profile_content(store, runtime)


def test_compact_memory_dedupes_slots_and_preserves_distinct_responsibilities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(
        store,
        runtime,
        "# Employee Memory\n"
        "- [preference] 企微回复偏好简洁、分点的回复方式\n"
        "- [preference|slot=reply_style] 偏好简洁、分点的回复方式\n"
        "- [work_context|slot=responsibility_ai] 负责 AI 架构工作\n"
        "- [work_context|slot=responsibility.ai_arch] 负责 AI 架构工作\n"
        "- [work_context|slot=responsibility] 负责数据架构工作\n"
        "- [work_context|slot=responsibility] 负责 AI 架构工作\n"
        "- [work_context|slot=travel_plan] 2026-07-12 去北京出差\n"
        "- [work_context|slot=travel_plan] 2026-07-13 去深圳出差\n",
    )
    _set_latest_user_message(runtime, "请清理重复记忆")

    result = tools["compact_employee_memory"].func(runtime=runtime)

    profile = _profile_content(store, runtime)
    assert result == "Compacted durable employee memory: removed 3 redundant or expired entries; 5 entries remain."
    assert _memory_lines(profile, "preference") == [
        "- [preference|slot=reply_style] 偏好简洁、分点的回复方式"
    ]
    assert "responsibility_ai" not in profile
    assert profile.count("slot=responsibility.ai_arch") == 1
    assert "- [work_context|slot=responsibility] 负责数据架构工作" in profile
    assert "- [work_context|slot=responsibility] 负责 AI 架构工作" in profile
    assert "2026-07-12 去北京出差" not in profile
    assert "2026-07-13 去深圳出差" in profile


def test_compact_memory_refuses_without_explicit_request(tmp_path: Any) -> None:
    store, runtime, tools = _memory_harness(tmp_path)
    original = "# Employee Memory\n- [preference] 偏好简洁回复\n"
    _put_profile(store, runtime, original)
    _set_latest_user_message(runtime, "我的长期记忆有哪些？")

    result = tools["compact_employee_memory"].func(runtime=runtime)

    assert result.startswith("Refused:")
    assert _profile_content(store, runtime) == original


def test_compact_memory_optionally_removes_expired_absolute_dates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(long_term_memory, "_today_utc", lambda: date(2026, 7, 10))
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(
        store,
        runtime,
        "# Employee Memory\n"
        "- [work_context|slot=meeting_plan] 2026年7月9日参加数据治理会议\n"
        "- [work_context|slot=travel_plan] 2026-07-12 去深圳出差\n"
        "- [work_context|slot=responsibility.data_arch] 负责数据架构工作\n",
    )
    _set_latest_user_message(runtime, "请清理过期记忆和历史行程")

    result = tools["compact_employee_memory"].func(
        remove_expired_temporal=True,
        runtime=runtime,
    )

    profile = _profile_content(store, runtime)
    assert "removed 1" in result
    assert "2026年7月9日" not in profile
    assert "2026-07-12 去深圳出差" in profile
    assert "负责数据架构工作" in profile


def test_compact_memory_refuses_expired_deletion_without_explicit_expired_request(tmp_path: Any) -> None:
    store, runtime, tools = _memory_harness(tmp_path)
    original = "# Employee Memory\n- [work_context|slot=travel_plan] 2026-07-01 去深圳出差\n"
    _put_profile(store, runtime, original)
    _set_latest_user_message(runtime, "请清理重复记忆")

    result = tools["compact_employee_memory"].func(
        remove_expired_temporal=True,
        runtime=runtime,
    )

    assert result.startswith("Refused:")
    assert _profile_content(store, runtime) == original


def test_concurrent_forget_calls_serialize_profile_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    _slow_profile_put(monkeypatch)
    store, runtime, tools = _memory_harness(tmp_path)
    _put_profile(
        store,
        runtime,
        "# Employee Memory\n"
        "- [work_context] 清理旧的北京出差记录\n"
        "- [work_context] 清理旧的深圳出差记录\n",
    )
    _set_latest_user_message(runtime, "请删除旧的北京和深圳出差记录")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                tools["forget_employee_memory"].func,
                memory=memory,
                runtime=runtime,
            )
            for memory in ("清理旧的北京出差记录", "清理旧的深圳出差记录")
        ]
        results = [future.result(timeout=5) for future in futures]

    profile = _profile_content(store, runtime)
    assert results == [
        "The matching durable employee memory has been removed.",
        "The matching durable employee memory has been removed.",
    ]
    assert "北京出差" not in profile
    assert "深圳出差" not in profile


def test_concurrent_remember_calls_serialize_profile_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD", raising=False)
    _slow_profile_put(monkeypatch)
    store, runtime, tools = _memory_harness(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                tools["remember_employee_memory"].func,
                memory=memory,
                category="work_context",
                slot=slot,
                runtime=runtime,
            )
            for memory, slot in (
                ("负责数据架构工作", "responsibility"),
                ("明天参加数据治理评审会", "meeting_plan"),
            )
        ]
        results = [future.result(timeout=5) for future in futures]

    profile = _profile_content(store, runtime)
    assert results == [
        "The requested durable employee memory has been recorded.",
        "The requested durable employee memory has been recorded.",
    ]
    assert "- [work_context|slot=responsibility] 负责数据架构工作" in profile
    assert "- [work_context|slot=meeting_plan] 明天参加数据治理评审会" in profile


def _memory_harness(
    tmp_path: Any,
) -> tuple[SQLiteStore, TestToolRuntime, dict[str, Any]]:
    store = SQLiteStore(tmp_path / "enterprise-store.sqlite3")
    context = _runtime_context()
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id=None,
        store=store,
    )
    tools = {tool.name: tool for tool in employee_memory_tools()}
    return store, runtime, tools


def _runtime_context() -> Mapping[str, object]:
    settings = EnterpriseRuntimeSettings(tenant_id="wkzq", namespace_secret="test-secret")
    context = enterprise_runtime_context({"oa_account": "zhuchunlin"}, "wecom:zhuchunlin", settings=settings)
    assert context is not None
    return context


def _profile_content(store: SQLiteStore, runtime: TestToolRuntime) -> str:
    item = store.get(enterprise_filesystem_namespace(runtime), _PROFILE_PATH)
    assert item is not None
    return str(item.value["content"])


def _put_profile(store: SQLiteStore, runtime: TestToolRuntime, content: str) -> None:
    store.put(
        enterprise_filesystem_namespace(runtime),
        _PROFILE_PATH,
        {"content": content, "encoding": "utf-8", "modified_at": "2026-07-02T00:00:00+00:00"},
        index=False,
    )


def _set_latest_user_message(runtime: TestToolRuntime, content: str) -> None:
    runtime.state["messages"] = [HumanMessage(content=content)]


def _memory_line_count(content: str, category: str) -> int:
    return len(_memory_lines(content, category))


def _memory_lines(content: str, category: str) -> list[str]:
    return [
        line
        for line in content.splitlines()
        if line.startswith(f"- [{category}]") or line.startswith(f"- [{category}|")
    ]


def _slow_profile_put(monkeypatch: pytest.MonkeyPatch) -> None:
    original_put_profile = long_term_memory._put_profile

    def slow_put_profile(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.05)
        original_put_profile(*args, **kwargs)

    monkeypatch.setattr(long_term_memory, "_put_profile", slow_put_profile)
