"""Loguru-only application route; synthetic stores, no live gateway or VM."""
# ruff: noqa: F811

import asyncio
import hashlib
import os
import subprocess
import sys
import textwrap

import pytest
from enterprise_wecom_digital_employee.sandbox_remote import remote_csv_tools
from loguru import logger
from test_sandbox_remote import remote  # noqa: F401


def test_fresh_process_default_stdlib_warning_without_handlers():
    code = textwrap.dedent('''
        import asyncio
        import io
        import logging
        from types import SimpleNamespace
        from loguru import logger
        root = logging.getLogger()
        assert root.level == logging.WARNING and root.handlers == []
        from enterprise_wecom_digital_employee.sandbox_remote import remote_csv_tools
        standard = logging.getLogger("enterprise_wecom_digital_employee.sandbox_remote")
        assert standard.getEffectiveLevel() == logging.WARNING and standard.handlers == []
        stream = io.StringIO()
        logger.remove()
        sink = logger.add(stream, level="INFO", format="{message}", catch=False)
        count = len(logger._core.handlers)
        runtime = SimpleNamespace(context={"enterprise": {
            "tenant_key": "t", "user_key": "u", "session_key": "s"}}, state={},
            tool_call_id="secret-call-id")
        for _ in range(2):
            tools = remote_csv_tools(grant_for=lambda _: None, file_store=None, runner=None)
            result = asyncio.run(tools[1].coroutine(runtime))
            assert result["state"] == "unavailable_or_rejected"
        lines = stream.getvalue().splitlines()
        assert len(lines) == 4  # one start and one terminal per invocation
        assert all(line.startswith("sandbox_tool ") for line in lines)
        assert all("%s" not in line and "secret-call-id" not in line for line in lines)
        assert len(logger._core.handlers) == count
        assert root.level == logging.WARNING and root.handlers == []
        assert standard.getEffectiveLevel() == logging.WARNING and standard.handlers == []
        logger.remove(sink)
        print("LOGURU_ONLY_PASS")
    ''')
    # No .env loading or actual application startup. Only the imported tool runs
    # an invalid synthetic grant query, so there is no remote client or file IO.
    result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(),  # noqa: S603 -- fixed test program
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LOGURU_ONLY_PASS"


@pytest.mark.parametrize("operation", ["run", "query", "read"])
def test_each_tool_has_one_correlated_pair_at_info(remote, sandbox_tool_logs, operation):
    s = remote
    s.runtime.tool_call_id = "SECRET-call\npath/token"
    tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    if operation == "read":
        outcome = s.runner.execute(s.request, s.data)
        action = tools[2].coroutine(outcome.artifact_ref, s.runtime)
    elif operation == "run":
        action = tools[0].coroutine(s.record.file_id, s.request.instruction, s.runtime)
    else:
        action = tools[1].coroutine(s.runtime)
    result = asyncio.run(action)
    records = sandbox_tool_logs
    assert len(records) == 2
    messages = [r["message"] for r in records]
    assert all(r["level"].name == "INFO" and r["exception"] is None for r in records)
    assert "phase=start" in messages[0] and "phase=complete" in messages[1]
    event = lambda line: line.split("event=")[1].split()[0]
    assert event(messages[0]) == event(messages[1])
    assert "call_sha256=" + hashlib.sha256(s.runtime.tool_call_id.encode()).hexdigest() in messages[0]
    assert "request_sha256=" + hashlib.sha256(s.request.request_id.encode()).hexdigest() in messages[1]
    assert "state=" + result["state"] in messages[1]
    for secret in (s.runtime.tool_call_id, s.request.instruction, s.request.request_id,
                   s.owner, s.token, s.data.decode(), s.output.decode()):
        assert secret not in "\n".join(messages)
    assert all("\n" not in message for message in messages)


@pytest.mark.parametrize("failure", ["rejected", "cancelled", "failed"])
def test_exception_paths_keep_semantics_and_no_exception_body(remote, sandbox_tool_logs, failure):
    class FatalSynthetic(BaseException):
        pass
    error = {"rejected": ValueError, "cancelled": asyncio.CancelledError, "failed": FatalSynthetic}[failure]
    def grant(_):
        raise error("SECRET-token /private/path BODY")
    tools = remote_csv_tools(grant_for=grant, file_store=remote.files, runner=remote.runner)
    if failure == "rejected":
        out = asyncio.run(tools[1].coroutine(remote.runtime))
        assert out["state"] == "unavailable_or_rejected"
        phase = "complete"
    else:
        with pytest.raises(error):
            asyncio.run(tools[1].coroutine(remote.runtime))
        phase = failure
    assert len(sandbox_tool_logs) == 2
    assert f"phase={phase}" in sandbox_tool_logs[-1]["message"]
    assert all(r["level"].name == "INFO" and r["exception"] is None for r in sandbox_tool_logs)
    assert "SECRET" not in str([r["message"] for r in sandbox_tool_logs])
    assert remote.calls == remote.events == []


def test_application_controls_filter_no_module_reconfiguration(remote):
    info, warning = [], []
    a = logger.add(lambda m: info.append(m.record["message"]), level="INFO", format="{message}")
    b = logger.add(lambda m: warning.append(m.record["message"]), level="WARNING", format="{message}")
    try:
        count = len(logger._core.handlers)
        tools = remote_csv_tools(grant_for=lambda _: remote.grant, file_store=remote.files, runner=remote.runner)
        asyncio.run(tools[1].coroutine(remote.runtime))
        assert len(info) == 2 and warning == []
        assert len(logger._core.handlers) == count
    finally:
        logger.remove(a)
        logger.remove(b)
