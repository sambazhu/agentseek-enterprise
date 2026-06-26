"""Minimal LangChain spec used to smoke-test the WeCom enterprise runtime.

This binding intentionally avoids a real model call. It lets us verify:

- WeCom callback delivery reaches ``agentseek gateway``;
- ``agentseek-enterprise`` injects ``employee_context``;
- gateway output is written back to the WeCom stream response.
"""

from __future__ import annotations

import json
from typing import Any

from agentseek_langchain.spec import InvocationContext, RunnableSpec


class SmokeRunnable:
    def invoke(self, runnable_input: object, *, config: object | None = None) -> object:
        del config
        return runnable_input


def build_input(context: InvocationContext) -> dict[str, Any]:
    employee = context.state.get("employee_context")
    return {
        "prompt": context.prompt,
        "session_id": context.session_id,
        "employee_context": employee if isinstance(employee, dict) else None,
        "state_keys": sorted(str(key) for key in context.state if isinstance(key, str) and not key.startswith("_")),
    }


def parse_output(result: object) -> str:
    data = result if isinstance(result, dict) else {"result": str(result)}
    employee = data.get("employee_context") if isinstance(data.get("employee_context"), dict) else {}
    employee_name = employee.get("name") or employee.get("staff_name") or employee.get("oa_account") or "未识别"
    org_name = employee.get("organization_name") or employee.get("branch_name") or employee.get("department_name") or ""
    payload = {
        "ok": True,
        "message": f"企微链路联调成功，已识别员工：{employee_name}" + (f"（{org_name}）" if org_name else ""),
        "prompt": data.get("prompt"),
        "session_id": data.get("session_id"),
        "has_employee_context": bool(employee),
        "state_keys": data.get("state_keys"),
    }
    return json.dumps(payload, ensure_ascii=False)


SPEC = RunnableSpec(
    runnable=SmokeRunnable(),
    build_input=build_input,
    parse_output=parse_output,
)
