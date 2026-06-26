from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from agentseek_contextseek.plugin import (
    ContextSeekPlugin,
    _format_context_block,
    _inject_context,
)


@pytest.fixture(autouse=True)
def _default_contextseek_plugin_env(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_TENANT", "default")
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "session")
    monkeypatch.setenv("AGENTSEEK_CTX_INJECTION_MODE", "prompt")
    monkeypatch.setenv("AGENTSEEK_CTX_STORE_USER_TURNS", "false")
    monkeypatch.setenv("AGENTSEEK_CTX_SKIP_SENSITIVE_CONTENT", "true")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def test_inject_context_into_prompt():
    result = _inject_context("user query", "[ContextSeek]\n- fact")
    assert result.startswith("[ContextSeek]")
    assert "user query" in result


def test_format_context_block():
    hit = MagicMock()
    hit.item.stage.value = "knowledge"
    hit.item.summary = "distributed DB fact"
    block = _format_context_block([hit])
    assert "[RetrievedEmployeeSemanticMemory]" in block
    assert "distributed DB fact" in block


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------


def test_plugin_init_applies_env_aliases():
    with patch("agentseek_contextseek.plugin.apply_contextseek_env_aliases") as mock_apply:
        ContextSeekPlugin()
    mock_apply.assert_called_once()


def test_get_client_lazy_and_cached():
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    with patch("importlib.import_module") as mock_import:
        mock_mod = MagicMock()
        mock_mod.ContextSeek.from_settings.return_value = mock_client
        mock_import.return_value = mock_mod
        c1 = plugin._get_client()
        c2 = plugin._get_client()
    assert c1 is c2
    mock_mod.ContextSeek.from_settings.assert_called_once()


def test_get_client_returns_none_on_failure():
    plugin = ContextSeekPlugin()
    with patch("importlib.import_module", side_effect=Exception("no contextseek")):
        client = plugin._get_client()
    assert client is None


def test_scope_from_message_uses_session_by_default():
    plugin = ContextSeekPlugin()
    message = {"chat_id": "chat42"}
    scope = plugin._scope_from_message(message, "ses99")
    assert scope == "default/chat42/ses99"


# ---------------------------------------------------------------------------
# load_state + build_prompt hooks
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_load_state_only_publishes_the_default_session_scope():
    plugin = ContextSeekPlugin()
    state = await plugin.load_state(
        message={"content": "what is OceanBase?", "chat_id": "c1"},
        session_id="s1",
    )
    assert state["_contextseek_scope"] == "default/c1/s1"


@pytest.mark.anyio
async def test_load_state_leaves_enterprise_scope_for_build_prompt(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    plugin = ContextSeekPlugin()
    state = await plugin.load_state(message={"content": "hi"}, session_id="s1")
    assert state == {}


@pytest.mark.anyio
async def test_build_prompt_returns_none_when_client_unavailable():
    plugin = ContextSeekPlugin()
    plugin._client = None
    plugin._client_initialized = True

    state = {}
    result = await plugin.build_prompt(message={"content": "hi"}, session_id="s1", state=state)
    assert result is None
    assert state["_contextseek_scope"] == "default/local/s1"


# ---------------------------------------------------------------------------
# build_prompt + save_state hooks
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_prompt_injects_context():
    hit = MagicMock()
    hit.item.stage.value = "knowledge"
    hit.item.summary = "a relevant fact"

    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    mock_client.retrieve.return_value = [hit]
    plugin._client = mock_client
    plugin._client_initialized = True
    state = {}
    result = await plugin.build_prompt(
        message={"content": "what is OceanBase?"},
        session_id="s1",
        state=state,
    )
    assert result is not None
    assert result.startswith("[RetrievedEmployeeSemanticMemory]")
    assert "what is OceanBase?" in result
    assert "a relevant fact" in state["_contextseek_block"]


@pytest.mark.anyio
async def test_build_prompt_returns_none_without_context():
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    mock_client.retrieve.return_value = []
    plugin._client = mock_client
    plugin._client_initialized = True
    result = await plugin.build_prompt(
        message={"content": "hi"},
        session_id="s1",
        state={},
    )
    assert result is None


@pytest.mark.anyio
async def test_enterprise_scope_uses_anonymous_employee_keys_and_state_injection(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    monkeypatch.setenv("AGENTSEEK_CTX_INJECTION_MODE", "state")
    plugin = ContextSeekPlugin()
    hit = MagicMock()
    hit.item.stage.value = "knowledge"
    hit.item.summary = "employee-specific fact"
    mock_client = MagicMock()
    mock_client.retrieve.return_value = [hit]
    plugin._client = mock_client
    plugin._client_initialized = True
    state = _enterprise_state()

    result = await plugin.build_prompt(message={"content": "what do I prefer?"}, session_id="wecom:user", state=state)

    assert result is None
    scope = state["_contextseek_scope"]
    assert isinstance(scope, str)
    assert scope.startswith("enterprise/v1/hmac-")
    assert "employee-a" not in scope
    context_block = state["_contextseek_block"]
    assert isinstance(context_block, str)
    assert "employee-specific fact" in context_block


@pytest.mark.anyio
async def test_enterprise_scope_fails_closed_without_identity_state(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    plugin._client = mock_client
    plugin._client_initialized = True
    state = {}

    result = await plugin.build_prompt(message={"content": "hi"}, session_id="wecom:unknown", state=state)

    assert result is None
    assert state["_contextseek_scope_status"] == "identity_required"
    mock_client.retrieve.assert_not_called()


@pytest.mark.anyio
async def test_save_state_calls_add():
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    plugin._client = mock_client
    plugin._client_initialized = True

    await plugin.save_state(
        session_id="s1",
        state={"_contextseek_scope": "default/c1/s1"},
        message={"chat_id": "c1"},
        model_output="answer text",
    )
    mock_client.add.assert_called_once()
    call_kwargs = mock_client.add.call_args
    assert "agent-response" in call_kwargs.kwargs.get("tags", [])
    assert call_kwargs.kwargs["source"] == "agentseek://semantic/default/c1/s1"
    assert call_kwargs.kwargs["source_type"] == "agent_inference"


@pytest.mark.anyio
async def test_save_state_skips_empty_response():
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    plugin._client = mock_client
    plugin._client_initialized = True

    await plugin.save_state(
        session_id="s1",
        state={},
        message={"chat_id": "c1"},
        model_output="",
    )
    mock_client.add.assert_not_called()


@pytest.mark.anyio
async def test_save_state_stores_final_question_and_answer_for_enterprise_mode(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_STORE_USER_TURNS", "true")
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    plugin._client = mock_client
    plugin._client_initialized = True

    await plugin.save_state(
        session_id="wecom:user",
        state={"_contextseek_scope": "enterprise/v1/tenant/user/semantic"},
        message={"content": "Please remember my preferred reply style."},
        model_output="I will keep replies concise.",
    )

    content = mock_client.add.call_args.args[0]
    assert "Employee request:" in content
    assert "Assistant final response:" in content


@pytest.mark.anyio
async def test_save_state_skips_sensitive_looking_turns(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_STORE_USER_TURNS", "true")
    plugin = ContextSeekPlugin()
    mock_client = MagicMock()
    plugin._client = mock_client
    plugin._client_initialized = True

    await plugin.save_state(
        session_id="s1",
        state={"_contextseek_scope": "default/c1/s1"},
        message={"content": "my password is secret"},
        model_output="I cannot retain credentials.",
    )

    mock_client.add.assert_not_called()


def _enterprise_state() -> dict[str, object]:
    return {
        "_langgraph_runtime_context": {
            "enterprise": {
                "version": "v1",
                "tenant_key": f"hmac-{'a' * 64}",
                "user_key": f"hmac-{'b' * 64}",
            }
        }
    }
