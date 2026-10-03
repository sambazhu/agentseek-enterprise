import asyncio
from types import SimpleNamespace

from enterprise_wecom_digital_employee.sandbox_tools import sandbox_business_tools


def test_model_schema_has_no_identity_or_credentials():
    tools = sandbox_business_tools(resolve=lambda *a: None, backend_for=lambda *a: None)
    schema = tools[0].tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"input_ref", "instruction"}


def test_untrusted_resolution_stops_before_backend():
    called = []
    tools = sandbox_business_tools(resolve=lambda *a: None,
                                  backend_for=lambda *a: called.append(a))
    result = asyncio.run(tools[0].coroutine("file", "analyze", SimpleNamespace()))
    assert result == {"state": "unavailable_or_rejected", "retry_allowed": False}
    assert not called
