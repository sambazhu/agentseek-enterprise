"""Real DeepAgent graph, scripted model and TEST-ONLY local sandbox provider.

This proves software routing/ToolRuntime/persistence, NOT LLM decision quality,
CubeSandbox isolation or WeCom delivery. No remote model/network is contacted.
"""

import asyncio
from dataclasses import dataclass
import json
import shlex
import subprocess
import sys
import time

from agentseek_execution.csv_business import BusinessStore
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from deepagents import create_deep_agent
from deepagents.graph import DeepAgentState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from typing import NotRequired

from enterprise_wecom_digital_employee.sandbox_authorization import SandboxGrant, instruction_digest
from enterprise_wecom_digital_employee.sandbox_composition import csv_pilot_tools


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@dataclass
class Context:
    enterprise: dict


class State(DeepAgentState):
    current_files: NotRequired[list[dict]]


def test_model_tool_model_loop_and_ordinary_question_without_create(tmp_path):
    private = tmp_path / "business"
    private.mkdir(mode=0o700)
    store = BusinessStore(private.resolve())
    files = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    record = files.store_bytes(scope=FileScope("tenant", "user", "session"), filename="input.csv",
                               data=b"group,amount\nA,1\nA,2\nB,4\n", mime_type="text/csv")
    instruction = "按group分组汇总amount"
    grant = SandboxGrant("test-request", "tenant", "user", "session", record.file_id,
                         instruction_digest(instruction), time.time() + 120)
    events = []

    class SyntheticProvider:
        def validate_request(self, request, data):
            pass  # Synthetic provider only; no real creation approval.

        def create(self, attempt):
            events.append("create")

        def run(self, attempt, command, timeout):
            events.append("execute")
            args = shlex.split(command)
            return subprocess.run([sys.executable, *args[1:]], capture_output=True, check=True,
                                  timeout=timeout, env={}).stdout.decode()

        def destroy(self, attempt):
            events.append("destroy")
            return True

    tools = csv_pilot_tools(grant_for=lambda runtime: grant, file_store=files,
                           business_store=store, provider_for=lambda request: SyntheticProvider())
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[dict(name="run_sandbox_task", id="call-1",
            args=dict(input_ref=record.file_id, instruction=instruction), type="tool_call")]),
        AIMessage(content="结果已持久化，沙箱已回收。"),
    ])
    graph = create_deep_agent(model=model, tools=tools, context_schema=Context, state_schema=State)
    context = Context(dict(tenant_key="tenant", user_key="user", session_key="session"))
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=instruction)],
                                       "current_files": [record.to_dict()]}, context=context))
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    outcome = json.loads(tool_messages[0].content)
    assert outcome["state"] == "succeeded" and outcome["cleanup_confirmed"]
    assert events == ["create", "execute", "destroy"]
    assert result["messages"][-1].content == "结果已持久化，沙箱已回收。"

    # Second real graph cycle reads durable result without recreating a sandbox.
    reader = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[dict(name="read_sandbox_csv_result", id="call-2",
            args=dict(artifact_ref=outcome["artifact_ref"]), type="tool_call")]),
        AIMessage(content="A合计3，B合计4。"),
    ])
    read_graph = create_deep_agent(model=reader, tools=tools, context_schema=Context)
    read_result = asyncio.run(read_graph.ainvoke({"messages": [HumanMessage(content="查看结果")]}, context=context))
    data = json.loads(next(m.content for m in read_result["messages"] if isinstance(m, ToolMessage)))
    assert data["csv"] == "group,total\nA,3\nB,4\n"
    assert events == ["create", "execute", "destroy"]

    normal = create_deep_agent(model=ScriptedModel(responses=[AIMessage(content="你好")]), tools=tools,
                               context_schema=Context)
    answer = asyncio.run(normal.ainvoke({"messages": [HumanMessage(content="你好")]}, context=context))
    assert answer["messages"][-1].content == "你好"
    assert events == ["create", "execute", "destroy"]
