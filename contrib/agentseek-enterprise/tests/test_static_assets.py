from __future__ import annotations

from pathlib import Path

import pytest
from agentseek_enterprise.static_assets import (
    STATIC_AGENT_INSTRUCTIONS_PATH,
    STATIC_SKILLS_ROOT,
    load_static_agent_assets,
)


def _project_root(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text("Trusted instructions", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "office-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: office-workflow\n---\nTrusted skill", encoding="utf-8")
    return tmp_path


def test_static_assets_only_expose_declared_agent_and_skill_files(tmp_path: Path) -> None:
    root = _project_root(tmp_path)
    (root / ".env").write_text("SECRET=not-an-agent-asset", encoding="utf-8")
    (root / "unrelated.txt").write_text("not an agent asset", encoding="utf-8")

    assets = load_static_agent_assets(root)

    assert assets.agent_instructions == "Trusted instructions"
    assert assets.files[STATIC_AGENT_INSTRUCTIONS_PATH]["content"] == "Trusted instructions"
    assert assets.files[f"{STATIC_SKILLS_ROOT}/office-workflow/SKILL.md"]["content"].endswith("Trusted skill")
    assert all(".env" not in path and "unrelated.txt" not in path for path in assets.files)


def test_static_asset_payload_is_copied_for_each_invocation(tmp_path: Path) -> None:
    assets = load_static_agent_assets(_project_root(tmp_path))

    first = assets.files_for_invocation()
    second = assets.files_for_invocation()
    first[STATIC_AGENT_INSTRUCTIONS_PATH]["content"] = "mutated"

    assert second[STATIC_AGENT_INSTRUCTIONS_PATH]["content"] == "Trusted instructions"
    assert assets.files[STATIC_AGENT_INSTRUCTIONS_PATH]["content"] == "Trusted instructions"


def test_static_assets_reject_hidden_skill_files(tmp_path: Path) -> None:
    root = _project_root(tmp_path)
    (root / "skills" / "office-workflow" / ".env").write_text("SECRET=not-an-agent-asset", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must not be hidden"):
        load_static_agent_assets(root)
