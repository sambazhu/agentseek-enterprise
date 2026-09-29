"""Deterministic response boundary for the exact server-approved CSV action.

No keyword filtering of model prose. Ordinary questions and explicit historical
file requests retain the normal reply path. No network or execution is initiated.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from agentseek_langchain.spec import invoke_runnable

from .sandbox_authorization import instruction_digest, runtime_scope, scoped_owner


def guarded_spec(spec, *, grant_for, runner):  # noqa: C901
    def binding(context):
        runtime = SimpleNamespace(context=context.runtime_context, state=context.state)
        grant = None
        try:
            candidate = grant_for(runtime)
            scope = runtime_scope(runtime)
            text = context.state.get("latest_user_message") or context.prompt
            if (isinstance(text, str) and instruction_digest(text) == candidate.instruction_sha256
                    and scope == (candidate.tenant_key, candidate.user_key, candidate.session_key)):
                grant = candidate
        except Exception:
            grant = None  # No raw grant/catalog errors enter replies or logs.
        return runtime, grant

    def build_input(context):
        runtime, grant = binding(context)
        # The guard binding never enters model-editable graph state.
        return spec.build_input(context), runtime, grant

    class GuardedRunnable:
        async def ainvoke(self, payload, config=None, context=None):
            value, runtime, grant = payload
            result = await invoke_runnable(spec.runnable, value, config, runtime_context=context)
            if grant is None:
                return result, None
            try:
                request = await asyncio.to_thread(runner.request_for_grant, scoped_owner(runtime_scope(runtime)), grant)
                outcome = await asyncio.to_thread(runner.store.snapshot, request) if request else None
                if outcome is None:
                    reply = "本轮请求尚未执行；历史任务或同名文件不能作为本轮成果。未确认本轮工作区回写或文件投递。"
                elif outcome.state == "succeeded" and outcome.cleanup_confirmed:
                    # Verify durable bytes, not the model's claim or a prior ToolMessage.
                    data = await asyncio.to_thread(runner.store.read, request.owner_id, outcome.artifact_ref)
                    reply = "本轮沙箱执行成功，结果已持久化，清理已确认。工作区回写与文件投递须分别核验，不能据此认定文件已发送。"
                    reply += "\n本轮持久化 CSV：\n" + data.decode("utf-8")
                elif outcome.state == "failed":
                    reply = "本轮沙箱任务失败；不得用历史成功结果替代，也不会自动重试。"
                else:
                    reply = "本轮任务状态未确定；不能声明成功，不得重复创建。请只读查询状态。"
                reply += f" request_id={grant.request_id}"
                if outcome is not None:
                    reply += f" attempt={outcome.attempt}"
            except Exception:
                reply = "本轮执行证据暂不可核验；不能声明成功，不得自动重试。"
            return result, reply

    def parse_output(result):
        original, authoritative = result
        return authoritative if authoritative is not None else spec.parse_output(original)

    def direct_response(context):
        if binding(context)[1] is not None:
            return None
        return spec.direct_response(context) if spec.direct_response is not None else None

    # Buffer until the evidence check. No streaming model prose bypasses it.
    return replace(spec, runnable=GuardedRunnable(), build_input=build_input,
                   parse_output=parse_output, stream_output=None, direct_response=direct_response)
