"""Real graph -> to_thread -> HTTPX -> loopback TLS, no real model or Cube."""

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from agentseek_execution.business_http import BusinessHttpClient, make_tls_server
from enterprise_wecom_digital_employee.sandbox_remote import remote_csv_tools
from test_sandbox_remote import remote
from test_sandbox_deepagent_loop import Context, State, ScriptedModel


@pytest.fixture
def tls_graph(remote, tmp_path):
    s = remote
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-only-loopback")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256()))
    ca, private = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM)); ca.chmod(0o600)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())); private.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    s.reached, s.release, s.completed = threading.Event(), threading.Event(), threading.Event()
    s.delay_response = False
    class Broker:
        def dispatch(self, *args):
            value = s.broker.dispatch(*args)
            s.reached.set()  # provider has already run before delaying the response
            try:
                if s.delay_response:
                    assert s.release.wait(4), "test must release server"
                return value
            finally:
                s.completed.set()
    server = make_tls_server(("127.0.0.1", 0), tls_context=context, broker=Broker())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    s.http = BusinessHttpClient(endpoint=f"https://127.0.0.1:{server.server_port}", token=s.token, ca_file=ca)
    s.runner.client = s.http
    s.tools = remote_csv_tools(grant_for=lambda _: s.grant, file_store=s.files, runner=s.runner)
    args = dict(input_ref=s.record.file_id, instruction=s.request.instruction)
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[dict(name="get_sandbox_task_result", id="check", type="tool_call", args=args)]),
        AIMessage(content="", tool_calls=[dict(name="run_sandbox_task", id="run", type="tool_call", args=args)]),
        AIMessage(content="done")])
    s.graph = create_deep_agent(model=model, tools=s.tools, context_schema=Context, state_schema=State)
    s.graph_input = {"messages": [HumanMessage(content=s.request.instruction)],
                     "current_files": [s.record.to_dict()]}
    try:
        yield s
    finally:
        s.release.set()
        if s.reached.is_set(): assert s.completed.wait(3)
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert not thread.is_alive()


def exchange_logs(caplog):
    return [json.loads(r.message.split("business_exchange ", 1)[1])
            for r in caplog.records if r.message.startswith("business_exchange ")]


def test_real_https_graph_success_and_redacted_phase_trace(tls_graph, caplog):
    s = tls_graph
    result = asyncio.run(s.graph.ainvoke(s.graph_input, context=s.context))
    outputs = [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
    assert outputs[0]["grant_available"]
    assert outputs[1]["workspace"]["state"] == "available"
    assert s.events == ["create", "execute", "destroy"]
    records = exchange_logs(caplog)
    stages = [r["stage"] for r in records]
    assert stages.index("tcp") < stages.index("tls") < stages.index("send_headers") < stages.index("response_headers")
    assert records[-1]["status"] == "succeeded"
    assert len({r["exchange_id"] for r in records}) == 1
    for secret in (s.token, s.http.endpoint, s.request.instruction, s.data.decode()):
        assert secret not in caplog.text


@pytest.mark.parametrize("failure", ["tls_certificate", "after_tcp_before_tls"])
def test_real_https_graph_setup_failure_is_visible(tls_graph, caplog, monkeypatch, failure):
    s = tls_graph
    if failure == "tls_certificate":
        s.http._tls = ssl.create_default_context()  # does not trust synthetic cert
    else:
        original = ssl.SSLContext.set_alpn_protocols
        def injected(context, protocols):
            if context is s.http._tls:
                raise RuntimeError("synthetic-secret-ALPN-error")
            return original(context, protocols)
        monkeypatch.setattr(ssl.SSLContext, "set_alpn_protocols", injected)
    asyncio.run(s.graph.ainvoke(s.graph_input, context=s.context))
    last = exchange_logs(caplog)[-1]
    assert last["stage"] == ("tls" if failure == "tls_certificate" else "tcp")
    assert last["status"] == ("not_sent" if failure == "tls_certificate" else "unknown")
    assert s.gateway.snapshot(s.request).state == ("failed" if failure == "tls_certificate" else "reconciling")
    assert not s.events and not s.reached.is_set()
    assert "synthetic-secret-ALPN-error" not in caplog.text


def test_real_https_graph_response_timeout_then_read_only_recovery(tls_graph, monkeypatch, caplog):
    import httpx
    s = tls_graph
    s.delay_response = True
    original = httpx.Client
    def short_read(*args, **kwargs):
        kwargs["timeout"] = httpx.Timeout(0.1, connect=2, write=2, pool=2)
        return original(*args, **kwargs)
    # Test-only acceleration; production client budgets are not changed.
    monkeypatch.setattr(httpx, "Client", short_read)
    asyncio.run(s.graph.ainvoke(s.graph_input, context=s.context))
    assert s.reached.is_set() and s.gateway.snapshot(s.request).state == "reconciling"
    assert exchange_logs(caplog)[-1]["error"] == "ReadTimeout"
    s.release.set()
    assert s.completed.wait(3)
    s.delay_response = False
    monkeypatch.setattr(httpx, "Client", original)
    recovered = asyncio.run(s.tools[1].coroutine(s.runtime))
    assert recovered["workspace"]["state"] == "available"
    assert s.events == ["create", "execute", "destroy"]


def test_graph_cancellation_does_not_cancel_thread_or_authorize_retry(tls_graph, caplog):
    s = tls_graph
    s.delay_response = True
    async def scenario():
        task = asyncio.create_task(s.graph.ainvoke(s.graph_input, context=s.context))
        try:
            assert await asyncio.to_thread(s.reached.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            # HTTP thread is still awaiting the first response; no second attempt allowed.
            assert s.gateway.snapshot(s.request).state == "reconciling"
            assert not s.runner.grant_unused(s.owner, "another-request")
        finally:
            s.release.set()
    asyncio.run(scenario())  # shutdown_default_executor waits for original worker
    assert s.gateway.snapshot(s.request).state == "succeeded"
    recovered = asyncio.run(s.tools[1].coroutine(s.runtime))
    assert recovered["workspace"]["state"] == "available"
    assert s.events == ["create", "execute", "destroy"]
    assert "stage=execute_thread error=CancelledError" in caplog.text
