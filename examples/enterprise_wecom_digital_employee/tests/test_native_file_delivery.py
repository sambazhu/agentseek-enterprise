"""Synthetic channel -> private handle -> tools -> HTTP mock -> durable receipt."""

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_wecom.addressing import ConversationAddress
from agentseek_wecom.channel import WeComChannel, _file_scope, _WeComInboundMessage
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.durable import SqliteDurableMessageStore
from agentseek_wecom.file_delivery import STATE_KEY, file_ref
from agentseek_wecom.plugin import WeComPlugin
from agentseek_wecom.transports.application import WeComAppTransport, WeComAppVisibility
from enterprise_wecom_digital_employee.native_file_delivery import native_file_tools
from pydantic import SecretStr


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSEEK_FILES_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_TENANT_ID", "synthetic-tenant")
    directory = tmp_path / "deliveries"
    directory.mkdir(mode=0o700)
    settings = WeComSettings(
        _env_file=None,
        enabled=False,
        corp_id="corp",
        app_transport_enabled=True,
        app_agent_id="123",
        app_transport_secret=SecretStr("SYNTHETIC_SECRET"),
        app_callback_token=SecretStr("SyntheticToken"),
        app_callback_encoding_aes_key=SecretStr("abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"),
        app_allowed_digital_employee_ids="assistant",
        app_default_digital_employee_id="assistant",
        durable_mode="sqlite",
        durable_sqlite_path=str(tmp_path / "outbox.sqlite"),
        durable_secret=SecretStr("synthetic-private-secret-for-tests-123456"),
        native_file_delivery_enabled=True,
        native_file_delivery_directory=str(directory),
    )
    calls = []
    mode = {"fail_send": False}

    def handler(request):
        calls.append(request)
        if request.url.path == "/cgi-bin/media/upload":
            return httpx.Response(200, json={"errcode": 0, "media_id": "SECRET_MEDIA"})
        assert request.url.path == "/cgi-bin/message/send"
        payload = json.loads(request.content)
        assert payload["touser"] == "employee"
        assert payload["msgtype"] == "file"
        if mode["fail_send"]:
            raise httpx.ReadTimeout("SECRET exception body", request=request)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = WeComAppTransport(settings=settings, tenant_id="synthetic-tenant", client=client)
    transport._access_token = "SECRET_TOKEN"  # noqa: S105 - synthetic HTTP mock only
    transport._access_token_expires_at = float("inf")
    transport._visibility = WeComAppVisibility(
        users=frozenset({"employee"}), parties=frozenset(), tags=frozenset(), expires_at=float("inf")
    )
    durable = SqliteDurableMessageStore(path=tmp_path / "outbox.sqlite", secret=settings.durable_secret)
    channel = WeComChannel(on_receive=None, settings=settings, app_transport=transport, durable_store=durable)
    address = ConversationAddress(
        "synthetic-tenant", "123", "wecom_app", "single", "employee", "employee", "employee", datetime.now(UTC), None
    )
    scope = _file_scope(
        tenant_id="synthetic-tenant",
        employee_id="employee",
        session_id=address.session_id,
        channel="wecom",
        chat_id=None,
        message_id=None,
    )
    files = LocalFileStore(FilesSettings.from_env())
    record = files.store_bytes(scope=scope, filename="summary.csv", data=b"group,total\nA,1\n", direction="outbound")
    message = _WeComInboundMessage(
        session_id=address.session_id,
        channel="wecom",
        chat_id="employee",
        content="把 summary.csv 发给我",
        context={"_agentseek_wecom_internal": {"message_id": "inbound-1"}},
    )
    message._agentseek_wecom_conversation_address = address
    return SimpleNamespace(
        channel=channel,
        message=message,
        scope=scope,
        record=record,
        calls=calls,
        client=client,
        durable=durable,
        mode=mode,
    )


async def get_runtime(rig):
    await rig.channel._bind_native_file_delivery(rig.message)
    plugin = WeComPlugin.__new__(WeComPlugin)
    state = plugin.load_state(rig.message, rig.message.session_id)
    return SimpleNamespace(
        state=state,
        context={
            "enterprise": {
                "tenant_key": rig.scope.tenant_key,
                "user_key": rig.scope.employee_key,
                "session_key": rig.scope.session_key,
            }
        },
    )


def test_channel_to_tools_to_real_outbox(rig):
    async def run():
        runtime = await get_runtime(rig)
        listing, deliver = native_file_tools()
        files = listing.func(runtime)["files"]
        assert files[0]["file_ref"] == file_ref(rig.record)
        assert "recipient" not in deliver.args and "runtime" not in deliver.tool_call_schema.model_fields
        assert runtime.state[STATE_KEY] not in rig.message.context_str
        result = await deliver.coroutine(files[0]["file_ref"], runtime)
        duplicate = await deliver.coroutine(files[0]["file_ref"], runtime)
        assert duplicate == dict(result, reused_receipt=True, delivery_notice="已有投递记录，本次未再次发送。")
        assert result["status"] == "api_accepted"
        assert result["user_receipt_confirmed"] is False
        assert len(rig.calls) == 2
        assert "SECRET" not in json.dumps(result)
        await rig.client.aclose()

    asyncio.run(run())


def test_send_timeout_and_outbox_recovery_never_resend(rig):
    async def run():
        rig.mode["fail_send"] = True
        runtime = await get_runtime(rig)
        deliver = native_file_tools()[1]
        result = await deliver.coroutine(file_ref(rig.record), runtime)
        assert result["status"] == "uncertain"
        # Simulate process restart/recovery with every recoverable status. The
        # manual flag is persisted in the encrypted outbox envelope.
        with closing(rig.durable._connection()) as db:
            row = db.execute("SELECT outbox_id FROM wecom_outbox").fetchone()
            assert row is not None
        for status in ("pending", "failed", "sending", "sent"):
            with closing(rig.durable._connection()) as db, db:
                db.execute("UPDATE wecom_outbox SET status=?, lease_expires_at=NULL", (status,))
            records = rig.durable.claim_recoverable_outbox(
                now=datetime.now(UTC) + timedelta(hours=1),
                owner="restarted",
                lease_duration=timedelta(seconds=30),
                limit=10,
            )
            assert len(records) == 1
            assert records[0].envelope["manual_recovery_only"] is True
            await rig.channel._recover_application_outbox(records[0])
        duplicate = await deliver.coroutine(file_ref(rig.record), runtime)
        assert duplicate == dict(result, reused_receipt=True, delivery_notice="已有投递记录，本次未再次发送。")
        assert len(rig.calls) == 2
        await rig.client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["unresolved", "group", "message_id", "foreign_scope", "implicit", "forged_state"])
def test_fail_closed_identity_and_authority(rig, case):
    async def run():
        address = rig.message._agentseek_wecom_conversation_address
        if case == "unresolved":
            rig.message._agentseek_wecom_conversation_address = replace(address, plaintext_userid=None)
        elif case == "group":
            rig.message._agentseek_wecom_conversation_address = replace(address, chat_type="group")
        elif case == "message_id":
            rig.message.context.clear()
        elif case == "implicit":
            rig.message.content = "请执行 CSV 然后自动发送"
        runtime = await get_runtime(rig)
        if case == "foreign_scope":
            runtime.context["enterprise"]["user_key"] = "sha256-" + "d" * 64
        if case == "forged_state":
            runtime.state[STATE_KEY] = "f" * 64
        # A model-created confirmation in mutable state never substitutes for
        # the original channel message captured in the private capability.
        runtime.state["latest_user_message"] = "把 summary.csv 发给我"
        runtime.state["confirmed"] = True
        result = await native_file_tools()[1].coroutine(file_ref(rig.record), runtime)
        assert result["status"] == "denied" and rig.calls == []
        await rig.client.aclose()

    asyncio.run(run())


def test_plugin_clears_previous_capability():
    plugin = WeComPlugin.__new__(WeComPlugin)
    assert plugin.load_state({"context": {}}, "session") == {STATE_KEY: ""}


def test_settings_default_off_and_dependencies():
    assert WeComSettings(_env_file=None).native_file_delivery_enabled is False
    with pytest.raises(ValueError, match="native file delivery requires"):
        WeComSettings(_env_file=None, native_file_delivery_enabled=True)


def test_real_deepagent_toolruntime_with_channel_capability(rig):
    from deepagents import create_deep_agent
    from enterprise_wecom_digital_employee.agent import EnterpriseAgentRuntimeContext, EnterpriseAgentState
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    async def run():
        runtime = await get_runtime(rig)
        model = Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "deliver_workspace_file",
                            "id": "call-1",
                            "args": {"file_ref": file_ref(rig.record)},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="企微接口已接受，请确认是否收到。"),
            ]
        )
        graph = create_deep_agent(
            model=model,
            tools=native_file_tools(),
            state_schema=EnterpriseAgentState,
            context_schema=EnterpriseAgentRuntimeContext,
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=rig.message.content)], **runtime.state},
            context=EnterpriseAgentRuntimeContext(enterprise=runtime.context["enterprise"]),
        )
        outcome = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
        assert outcome["status"] == "api_accepted" and len(rig.calls) == 2
        assert "SECRET" not in str(result["messages"])
        await rig.client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_receive_hook_issues_capability_only_when_enabled(rig, enabled):
    async def run():
        seen = []
        rig.channel.settings.native_file_delivery_enabled = enabled

        async def receive(message):
            state = WeComPlugin.__new__(WeComPlugin).load_state(message, message.session_id)
            seen.append(state[STATE_KEY])

        rig.channel.bind_receiver(receive)
        await rig.channel._dispatch_one(rig.message)
        assert len(seen) == 1 and bool(seen[0]) is enabled
        assert rig.calls == []  # binding/listing never uploads or sends
        await rig.client.aclose()

    asyncio.run(run())
