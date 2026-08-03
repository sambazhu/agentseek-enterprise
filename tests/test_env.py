from __future__ import annotations

import importlib
import os
import sys

from agentseek.env import (
    DEFAULT_AGENTSEEK_CONFIG,
    DEFAULT_AGENTSEEK_HOME,
    DEFAULT_PLUGIN_SANDBOX,
    agentseek_config_file,
    apply_agentseek_env_aliases,
    get_agentseek_settings,
)


def test_agentseek_env_fills_missing_bub_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUB_MODEL", raising=False)
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("BUB_PROJECT", raising=False)
    monkeypatch.setenv("AGENTSEEK_MODEL", "openai:test-model")

    apply_agentseek_env_aliases()

    assert os.environ["BUB_MODEL"] == "openai:test-model"


def test_existing_bub_env_takes_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUB_API_KEY", "bub-key")
    monkeypatch.setenv("AGENTSEEK_API_KEY", "agentseek-key")
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("BUB_PROJECT", raising=False)

    apply_agentseek_env_aliases()

    assert os.environ["BUB_API_KEY"] == "bub-key"


def test_agentseek_wecom_aliases_fill_missing_bub_settings(monkeypatch) -> None:
    expected_secret = "-".join(("test", "secret"))
    target_environ = {
        "AGENTSEEK_WECOM_BOT_ID": "bot-id",
        "AGENTSEEK_WECOM_SECRET": expected_secret,
        "AGENTSEEK_WECOM_DM_POLICY": "allowlist",
    }
    for name, value in target_environ.items():
        monkeypatch.setenv(name, value)

    apply_agentseek_env_aliases(target_environ)

    assert target_environ["BUB_WECOM_BOT_ID"] == "bot-id"
    assert target_environ["BUB_WECOM_SECRET"] == expected_secret
    assert target_environ["BUB_WECOM_DM_POLICY"] == "allowlist"


def test_native_bub_wecom_setting_takes_precedence_over_agentseek_alias(monkeypatch) -> None:
    target_environ = {
        "AGENTSEEK_WECOM_BOT_ID": "agentseek-bot-id",
        "BUB_WECOM_BOT_ID": "bub-bot-id",
    }
    monkeypatch.setenv("AGENTSEEK_WECOM_BOT_ID", target_environ["AGENTSEEK_WECOM_BOT_ID"])

    apply_agentseek_env_aliases(target_environ)

    assert target_environ["BUB_WECOM_BOT_ID"] == "bub-bot-id"


def test_agentseek_defaults_bub_home_to_agentseek_home(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("AGENTSEEK_HOME", raising=False)

    apply_agentseek_env_aliases()

    assert os.environ["BUB_HOME"] == str(tmp_path / DEFAULT_AGENTSEEK_HOME)
    assert agentseek_config_file() == (tmp_path / DEFAULT_AGENTSEEK_HOME / DEFAULT_AGENTSEEK_CONFIG).resolve()


def test_agentseek_home_alias_fills_missing_bub_home(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    agentseek_home = tmp_path / "agentseek-home"
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("BUB_PROJECT", raising=False)
    monkeypatch.setenv("AGENTSEEK_HOME", str(agentseek_home))

    apply_agentseek_env_aliases()

    assert os.environ["BUB_HOME"] == str(agentseek_home)


def test_existing_bub_home_takes_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    bub_home = tmp_path / "bub-home"
    agentseek_home = tmp_path / "agentseek-home"
    monkeypatch.setenv("BUB_HOME", str(bub_home))
    monkeypatch.delenv("BUB_PROJECT", raising=False)
    monkeypatch.setenv("AGENTSEEK_HOME", str(agentseek_home))

    apply_agentseek_env_aliases()

    assert os.environ["BUB_HOME"] == str(bub_home)


def test_agentseek_defaults_bub_project_under_home(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUB_PROJECT", raising=False)
    monkeypatch.delenv("AGENTSEEK_PROJECT", raising=False)
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("AGENTSEEK_HOME", raising=False)

    apply_agentseek_env_aliases()

    assert os.environ["BUB_PROJECT"] == str(tmp_path / DEFAULT_AGENTSEEK_HOME / DEFAULT_PLUGIN_SANDBOX)


def test_existing_bub_project_takes_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUB_PROJECT", str(tmp_path / "custom-project"))
    monkeypatch.delenv("AGENTSEEK_PROJECT", raising=False)
    monkeypatch.delenv("BUB_HOME", raising=False)

    apply_agentseek_env_aliases()

    assert os.environ["BUB_PROJECT"] == str(tmp_path / "custom-project")


def test_agentseek_dotenv_does_not_fill_missing_bub_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "AGENTSEEK_TAPESTORE_SQLALCHEMY_URL=sqlite+pysqlite:////tmp/agentseek.sqlite\n",
        encoding="utf-8",
    )
    target_environ: dict[str, str] = {}

    apply_agentseek_env_aliases(target_environ)

    assert "BUB_TAPESTORE_SQLALCHEMY_URL" not in target_environ


def test_importing_main_does_not_promote_dotenv_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTSEEK_SECRET", raising=False)
    monkeypatch.delenv("BUB_SECRET", raising=False)
    monkeypatch.delitem(sys.modules, "agentseek.__main__", raising=False)
    (tmp_path / ".env").write_text("AGENTSEEK_SECRET=dotenv-secret\n", encoding="utf-8")
    original_environ = os.environ.copy()

    importlib.import_module("agentseek.__main__")

    assert os.environ == original_environ


def test_agentseek_env_file_is_not_loaded_into_process_aliases(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTSEEK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "AGENTSEEK_ENV_FILE=project.env\n",
        encoding="utf-8",
    )
    (tmp_path / "project.env").write_text(
        "AGENTSEEK_MODEL=openai:project-model\n"
        "AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES=[\"vector\"]\n",
        encoding="utf-8",
    )
    target_environ: dict[str, str] = {}

    apply_agentseek_env_aliases(target_environ)

    assert "AGENTSEEK_MODEL" not in target_environ
    assert "BUB_MODEL" not in target_environ
    assert "AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES" not in target_environ


def test_explicit_env_takes_precedence_over_agentseek_env_file(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTSEEK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "AGENTSEEK_ENV_FILE=project.env\n",
        encoding="utf-8",
    )
    (tmp_path / "project.env").write_text(
        "AGENTSEEK_MODEL=openai:project-model\n",
        encoding="utf-8",
    )
    target_environ = {"AGENTSEEK_MODEL": "openai:explicit-model"}

    apply_agentseek_env_aliases(target_environ)

    assert target_environ["AGENTSEEK_MODEL"] == "openai:explicit-model"
    assert target_environ["BUB_MODEL"] == "openai:explicit-model"


def test_agentseek_settings_default_console_false(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSEEK_CONSOLE", raising=False)

    assert get_agentseek_settings().console is False


def test_agentseek_settings_reads_console_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSEEK_CONSOLE", "true")

    assert get_agentseek_settings().console is True


def test_apply_agentseek_env_aliases_updates_supplied_mapping(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    target_environ = {"AGENTSEEK_MODEL": "openai:test-model"}

    apply_agentseek_env_aliases(target_environ)

    assert target_environ["BUB_MODEL"] == "openai:test-model"
