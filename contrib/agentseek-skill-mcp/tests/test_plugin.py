from __future__ import annotations

from agentseek_skill_mcp.plugin import SkillMCPPlugin, main


def test_plugin_instantiation() -> None:
    plugin = SkillMCPPlugin()
    assert isinstance(plugin, SkillMCPPlugin)


def test_main_factory() -> None:
    plugin = main()
    assert isinstance(plugin, SkillMCPPlugin)


def test_plugin_no_hooks() -> None:
    hook_names = {
        "admit_message",
        "load_state",
        "save_state",
        "system_prompt",
        "build_prompt",
        "run_model",
    }
    for name in hook_names:
        assert not hasattr(SkillMCPPlugin, name), f"SkillMCPPlugin should not define {name}"
