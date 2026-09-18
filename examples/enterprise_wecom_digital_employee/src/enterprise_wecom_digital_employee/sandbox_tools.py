"""Explicit server injection for sandbox business tools; disabled by default.

Resolvers must derive ownership/request identity from authenticated runtime and
check input ownership plus exact-action approval, never from model arguments.
"""

import asyncio
from dataclasses import asdict
from typing import Callable

from langchain.tools import ToolRuntime, tool


def sandbox_business_tools(*, resolve: Callable, backend_for: Callable):
    """Trusted composition supplies both adapters; no environment auto-enable."""
    @tool
    async def run_sandbox_task(input_ref: str, instruction: str, runtime: ToolRuntime) -> dict:
        """Run the authorized CSV pilot in an isolated sandbox.

        Supports ONLY UTF-8 CSV columns group,amount: sum amount by group.
        This is not an arbitrary Python/shell execution tool.
        Use only when computation needs execution, not for ordinary questions.
        Input must be an available uploaded-file reference, never a host path.
        A succeeded result means output persisted and sandbox absence confirmed.
        On reconciling/failed do not retry or claim success.
        """
        # Lazy import keeps execution an optional, explicitly installed capability.
        try:
            from agentseek_execution.business_execution import BusinessRequest, run_business
            request = resolve(runtime, input_ref, instruction)
            if not isinstance(request, BusinessRequest):
                raise ValueError("trusted request required")
            # Construct thread-affine backend resources in the execution thread.
            result = await asyncio.to_thread(lambda: run_business(request, backend_for(request, runtime)))
            return asdict(result)
        except Exception:
            # Provider/config/auth exception text may contain private data.
            return {"state": "unavailable_or_rejected", "retry_allowed": False}

    return [run_sandbox_task]
