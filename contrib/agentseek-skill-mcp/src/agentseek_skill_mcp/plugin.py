from __future__ import annotations


class SkillMCPPlugin:
    """Bub plugin shell for Agent Skill MCP Platform integration.

    This plugin does not register any Bub hooks. All functionality is provided
    through the Python API in :mod:`agentseek_skill_mcp.loader`, which generated
    projects call during ``build_agent()``.

    The entry-point exists solely so Bub discovers and loads the package,
    making the SDK and configuration available to the runtime.
    """

    def __init__(self, framework: object | None = None) -> None:
        del framework


def main(framework: object | None = None) -> SkillMCPPlugin:
    """Bub entry-point factory."""
    return SkillMCPPlugin(framework)
