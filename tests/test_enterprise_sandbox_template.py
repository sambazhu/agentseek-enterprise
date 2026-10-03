"""Generated-project checks; synthetic local configuration, no live services.

These do not install dependencies or prove a new deployment's live acceptance.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from cookiecutter.main import cookiecutter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/enterprise_wecom_digital_employee"
MODULES = (
    "native_file_delivery",
    "sandbox_authorization",
    "sandbox_composition",
    "sandbox_remote",
    "sandbox_result_guard",
    "sandbox_spec",
    "sandbox_tools",
    "sandbox_workspace_binding",
    "tool_observation",
)
PACKAGE = "distribution_smoke"


@pytest.fixture
def generated(tmp_path, monkeypatch):
    path = Path(
        cookiecutter(
            str(ROOT / "templates/deepagents/enterprise-wecom"),
            no_input=True,
            output_dir=str(tmp_path),
            extra_context={"project_slug": PACKAGE},
        )
    )
    monkeypatch.syspath_prepend(str(path / "src"))
    monkeypatch.chdir(path)
    yield path
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            sys.modules.pop(name)


def test_generated_modules_match_verified_example_and_import(generated):
    for name in MODULES:
        source = (EXAMPLE / "src/enterprise_wecom_digital_employee" / f"{name}.py").read_text()
        rendered = (generated / "src" / PACKAGE / f"{name}.py").read_text()
        assert rendered == source.replace("enterprise_wecom_digital_employee", PACKAGE)
        ast.parse(rendered)
        assert importlib.import_module(f"{PACKAGE}.{name}").__file__.startswith(str(generated))
    agent_text = (generated / "src" / PACKAGE / "agent.py").read_text()
    assert "enterprise_wecom_digital_employee" not in agent_text
    assert "_get_skill_mcp_sync_result" not in agent_text  # Unrelated platform sync is not migrated.


def test_cli_can_create_fork_template_from_local_path(tmp_path):
    from typer.testing import CliRunner

    from tests.cli_commands.helpers import build_command_app

    result = CliRunner().invoke(
        build_command_app(),
        ["create", str(ROOT / "templates/deepagents/enterprise-wecom"), "--no-input", "--output-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    package = tmp_path / "enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee"
    assert all((package / f"{name}.py").is_file() for name in MODULES)


def test_default_env_and_lifecycle_are_fail_closed(generated):
    lifecycle = tomllib.loads((generated / ".agentseek/lifecycle.toml").read_text())["env"]
    assert lifecycle["AGENTSEEK_LANGCHAIN_SPEC"]["default"] == f"{PACKAGE}.agent:build_spec"
    assert lifecycle["AGENTSEEK_WECOM_NATIVE_FILE_DELIVERY_ENABLED"]["default"] == "false"
    for key in (
        "AGENTSEEK_WECOM_NATIVE_FILE_DELIVERY_DIRECTORY",
        "AGENTSEEK_SANDBOX_BUSINESS_CONFIG",
        "AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256",
    ):
        assert lifecycle[key]["required"] is False and "default" not in lifecycle[key]
    env = {}
    for line in (generated / ".env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            assert key not in env
            env[key] = value
    assert env["AGENTSEEK_LANGCHAIN_SPEC"] == f"{PACKAGE}.agent:build_spec"
    assert env["AGENTSEEK_WECOM_NATIVE_FILE_DELIVERY_ENABLED"] == "false"
    assert env["AGENTSEEK_WORKSPACE_DOWNLOAD_MODE"] == "disabled"
    assert not any(key.startswith("AGENTSEEK_SANDBOX_") for key in env)


@pytest.mark.parametrize("local", [False, True])
def test_execution_dependency_is_opt_in_for_git_and_local_sources(tmp_path, local):
    path = Path(
        cookiecutter(
            str(ROOT / "templates/deepagents/enterprise-wecom"),
            no_input=True,
            output_dir=str(tmp_path),
            extra_context={"project_slug": PACKAGE, "_agentseek_source_path_posix": str(ROOT) if local else ""},
        )
    )
    project = tomllib.loads((path / "pyproject.toml").read_text())
    assert not any(dep.startswith("agentseek-execution") for dep in project["project"]["dependencies"])
    assert project["project"]["optional-dependencies"]["sandbox"] == ["agentseek-execution[broker]"]
    source = project["tool"]["uv"]["sources"]["agentseek-execution"]
    assert source == (
        {"path": str(ROOT / "contrib/agentseek-execution"), "editable": True}
        if local
        else {
            "git": "https://github.com/sambazhu/agentseek-enterprise.git",
            "subdirectory": "contrib/agentseek-execution",
        }
    )


def test_real_default_graph_has_native_tools_but_no_sandbox(generated, monkeypatch, tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    module = importlib.import_module(f"{PACKAGE}.agent")
    monkeypatch.setenv("AGENTSEEK_WORK_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH", str(tmp_path / "memory.sqlite"))
    module.get_settings.cache_clear()
    settings = module.get_settings()
    monkeypatch.setattr(type(settings), "build_model", lambda self: Model(responses=[AIMessage(content="hello")]))
    try:
        tools = module.build_agent().nodes["tools"].bound.tools_by_name
        assert {"list_workspace_delivery_files", "deliver_workspace_file"} <= tools.keys()
        assert not {"run_sandbox_task", "get_sandbox_task_result", "read_sandbox_csv_result"} & tools.keys()
        # Missing native capability fails closed without attempting a send.
        native = importlib.import_module(f"{PACKAGE}.native_file_delivery").native_file_tools()
        runtime = SimpleNamespace(
            state={}, context={"enterprise": {"tenant_key": "t", "user_key": "u", "session_key": "s"}}
        )
        assert native[0].func(runtime=runtime) == {"status": "unavailable", "files": []}
    finally:
        module.get_settings.cache_clear()


@pytest.mark.parametrize("enabled", [False, True])
def test_generated_profile_must_grant_sandbox(generated, monkeypatch, enabled):
    module = importlib.import_module(f"{PACKAGE}.agent")
    calls = []
    monkeypatch.setattr(module, "build_agent", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(module, "RoutedAgentRunnable", lambda **kwargs: kwargs)
    registry = SimpleNamespace(
        profile=SimpleNamespace(tool_grants=("run_sandbox_task",) if enabled else ()),
        shared_capability_tools=lambda: [],
        playbook_refs=("report",),
        get=lambda _: object(),
    )
    tools = [object()]
    module._build_runtime_runnable(registry, sandbox_tools=tools)
    assert len(calls) == 2
    assert all((call.get("sandbox_tools") == tools) is enabled for call in calls)


def test_generated_spec_requires_explicit_config(generated, monkeypatch):
    monkeypatch.delenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", raising=False)
    module = importlib.import_module(f"{PACKAGE}.sandbox_spec")
    with pytest.raises(KeyError, match="AGENTSEEK_SANDBOX_BUSINESS_CONFIG"):
        module.build_spec()


@pytest.mark.parametrize("approved", [False, "true", 1])
def test_generated_spec_refuses_unapproved_config(generated, monkeypatch, tmp_path, approved):
    config = {
        "schema": 1,
        "approved": approved,
        "endpoint": "https://unused.invalid",
        "ca_file": "/unused/ca",
        "broker_token_file": "/unused/token",
        "grants_file": "/unused/grants",
        "grants_sha256": "0" * 64,
        "mirror_directory": "/unused/mirror",
    }
    path = tmp_path.resolve() / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", str(path))
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="unapproved"):
        importlib.import_module(f"{PACKAGE}.sandbox_spec").build_spec()


def test_generated_business_spec_assembles_tools_and_guard(generated, monkeypatch, tmp_path):
    from agentseek_execution import business_http
    from agentseek_files import store, workspace_download
    from agentseek_langchain.spec import RunnableSpec

    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    token = private / "token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    grants = private / "grants.json"
    grants.write_text(json.dumps({"schema": 1, "approved": True, "grants": []}))
    grants.chmod(0o600)
    config = {
        "schema": 1,
        "approved": True,
        "endpoint": "https://unused.invalid",
        "ca_file": str(token),
        "broker_token_file": str(token),
        "grants_file": str(grants),
        "grants_sha256": hashlib.sha256(grants.read_bytes()).hexdigest(),
        "mirror_directory": str(private),
    }
    path = private / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG", str(path))
    monkeypatch.setenv("AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(business_http, "BusinessHttpClient", lambda **kwargs: object())
    monkeypatch.setattr(store, "LocalFileStore", lambda settings: object())
    monkeypatch.setattr(workspace_download, "configured_workspace_downloads", lambda files: None)
    captured = []
    agent = importlib.import_module(f"{PACKAGE}.agent")
    monkeypatch.setattr(
        agent,
        "build_spec",
        lambda *, sandbox_tools: (
            captured.extend(sandbox_tools)
            or RunnableSpec(runnable=object(), build_input=lambda c: c.state, parse_output=str)
        ),
    )
    spec = importlib.import_module(f"{PACKAGE}.sandbox_spec").build_spec()
    assert {tool.name for tool in captured} == {
        "run_sandbox_task",
        "get_sandbox_task_result",
        "read_sandbox_csv_result",
    }
    assert type(spec.runnable).__name__ == "GuardedRunnable" and spec.stream_output is None
    assert spec.direct_response is not None


def test_generated_gateway_preserves_explicit_env_and_aliases(generated, monkeypatch, tmp_path):
    path = generated / "scripts/bub_gateway.py"
    spec = importlib.util.spec_from_file_location("generated_gateway_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / "baseline.env"
    env.write_text("AGENTSEEK_LANGCHAIN_SPEC=old.agent:build_spec\nBUB_LANGCHAIN_SPEC=old.agent:build_spec\n")
    monkeypatch.setenv("AGENTSEEK_ENV_FILE", str(env))
    monkeypatch.setenv("AGENTSEEK_LANGCHAIN_SPEC", f"{PACKAGE}.sandbox_spec:build_spec")
    monkeypatch.delenv("BUB_LANGCHAIN_SPEC", raising=False)
    # Restore every BUB alias added by the helper when this test completes.
    for key in list(module.os.environ):
        if key.startswith("AGENTSEEK_"):
            alias = "BUB_" + key.removeprefix("AGENTSEEK_")
            if alias not in module.os.environ:
                monkeypatch.delenv(alias, raising=False)
    module._load_project_env_file()
    module._apply_project_bub_aliases()
    assert module.os.environ["BUB_LANGCHAIN_SPEC"] == f"{PACKAGE}.sandbox_spec:build_spec"
    assert module.os.environ["AGENTSEEK_LANGCHAIN_SPEC"] == f"{PACKAGE}.sandbox_spec:build_spec"
