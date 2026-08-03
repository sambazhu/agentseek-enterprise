"""Regression checks for the upstream and enterprise WeCom boundaries."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_enterprise_workspace_uses_only_the_governed_wecom_adapter() -> None:
    """Avoid two packages registering the same ``bub/wecom`` entry point."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    plugins = pyproject["dependency-groups"]["plugins"]
    sources = pyproject["tool"]["uv"]["sources"]
    assert "agentseek-wecom" in plugins
    assert "bub-wecom" not in plugins
    assert sources["agentseek-wecom"] == {"workspace": True}
    assert "bub-wecom" not in sources


def test_generic_langchain_template_keeps_upstream_wecom_adapter() -> None:
    """The generic template may use Bub's basic adapter in isolation."""
    template_text = (
        ROOT
        / "templates"
        / "langchain"
        / "default"
        / "{{cookiecutter.project_slug}}"
        / "pyproject.toml"
    ).read_text(encoding="utf-8")

    assert '"bub-wecom"' in template_text
    assert 'subdirectory = "packages/bub-wecom"' in template_text


def test_wecom_env_docs_cover_native_credentials_and_access_policies() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "BUB_WECOM_BOT_ID",
        "BUB_WECOM_SECRET",
        "BUB_WECOM_WEBSOCKET_URL",
        "BUB_WECOM_DM_POLICY",
        "BUB_WECOM_ALLOW_FROM",
        "BUB_WECOM_GROUP_POLICY",
        "BUB_WECOM_GROUP_ALLOW_FROM",
    ):
        assert name in env_example
    assert "AGENTSEEK_WECOM_CALLBACK_PATH" in env_example
    assert "AGENTSEEK_WECOM_TOKEN" in env_example
