import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from agentseek_execution.cube_journal import CubeJournal
from agentseek_execution.cube_worker import perform
from agentseek_execution.models import ContractError


@pytest.fixture
def platform(tmp_path):  # noqa: C901 -- isolated stateful mock HTTP protocol fixture
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    key = os.urandom(32)
    key_file = tmp_path / "key"
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    control = tmp_path / "control"
    control.write_text("c" * 40)
    control.chmod(0o600)
    journal = CubeJournal(runtime, key)
    resource = uuid4().hex
    journal.insert(resource, {"resource_id": resource, "state": "creating", "lease": {"create_token": "token-one"}})
    state = {"receipt": None, "calls": [], "missing_token": False, "foreign": False, "duplicates": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, data):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, body))
            assert self.headers["X-API-Key"] == "c" * 40
            state["receipt"] = {
                "sandboxID": "cube-id",
                "templateID": "tpl-pinned",
                "domain": "example.invalid",
                "envdAccessToken": "private-envd",
                "metadata": body["metadata"],
                "state": "running",
            }
            if not state["missing_token"]:
                state["receipt"]["trafficAccessToken"] = "private-traffic"
            self.respond(201, state["receipt"])

        def do_GET(self):
            state["calls"].append(("GET", self.path))
            row = state["receipt"]
            if self.path == "/sandboxes":
                summary = None if row is None else {key: row[key] for key in ("sandboxID", "templateID", "state")}
                self.respond(200, [] if row is None else [summary] * (2 if state["duplicates"] else 1))
            elif row is None:
                self.respond(404, {"message": "not found"})
            else:
                result = dict(row)
                if state["foreign"]:
                    result["metadata"] = {"agentseek_run_id": "foreign-run"}
                self.respond(200, result)

        def do_DELETE(self):
            state["calls"].append(("DELETE", self.path))
            state["receipt"] = None
            self.respond(204, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "runtime": str(runtime),
        "encryption_key_file": str(key_file),
        "cube_key_file": str(control),
        "api_url": f"http://127.0.0.1:{server.server_port}",
        "template_id": "tpl-pinned",
        "proxy_port": 13080,
        "sandbox_domain": "example.invalid",
        "run_id": "m2-run",
    }

    # HTTP only in this isolated mock; daemon read_config rejects HTTP deployment.
    def call(operation):
        return perform({"config": config, "resource_id": resource, "operation": operation})

    yield call, state, journal, resource
    server.shutdown()
    server.server_close()
    thread.join()
    journal.close()


def test_real_sdk_create_receipt_rehydrate_and_confirmed_delete(platform):
    call, state, journal, resource = platform
    assert call("create") == {"status": "created"}
    body = state["calls"][0][2]
    assert body["templateID"] == "tpl-pinned"
    assert body["allow_internet_access"] is False
    assert body["network"]["allowPublicTraffic"] is False
    assert body["metadata"] == {"agentseek_run_id": "m2-run", "agentseek_create_token": "token-one"}
    assert journal.require_job(resource)["receipt"]["trafficAccessToken"] == "private-traffic"
    assert call("connect") == {"status": "running"}
    assert call("delete") == {"status": "stopped"}
    assert call("delete") == {"status": "stopped"}
    assert [item[0] for item in state["calls"]].count("POST") == 1
    assert [item[0] for item in state["calls"]].count("DELETE") == 1


def test_real_sdk_fixed_probe_preserves_traffic_header(platform, monkeypatch):
    from cubesandbox import Sandbox
    from cubesandbox._commands import CommandResult, Commands

    call, _, _, _ = platform
    call("create")
    observed = []
    original = Sandbox._traffic_token_headers

    def headers(self):
        result = original(self)
        observed.append(result)
        return result

    monkeypatch.setattr(Sandbox, "_traffic_token_headers", headers)

    def run(self, command, **kwargs):
        assert command == "printf 'M2_DIRECT_OK'" and kwargs["timeout"] == 5
        self._sandbox._traffic_token_headers()
        return CommandResult(stdout="M2_DIRECT_OK", stderr="", exit_code=0)

    monkeypatch.setattr(Commands, "run", run)
    assert call("direct") == {"status": "executed", "stdout": "M2_DIRECT_OK"}
    assert any(item.get("e2b-traffic-access-token") == "private-traffic" for item in observed)


def test_receipt_without_token_persisted_but_not_accepted(platform):
    call, state, journal, resource = platform
    state["missing_token"] = True
    with pytest.raises(ContractError):
        call("create")
    assert journal.require_job(resource)["receipt"]["sandboxID"] == "cube-id"
    assert call("delete") == {"status": "stopped"}


def test_foreign_metadata_prevents_delete(platform):
    call, state, _, _ = platform
    call("create")
    state["foreign"] = True
    with pytest.raises(ContractError):
        call("delete")
    assert not any(item[0] == "DELETE" for item in state["calls"])


@pytest.mark.parametrize("mode", ["one", "zero", "duplicate", "foreign"])
def test_lost_receipt_requires_unique_exact_server_marker(platform, mode):
    call, state, journal, resource = platform
    call("create")
    journal.patch(resource, receipt=None)
    if mode == "zero":
        state["receipt"] = None
    elif mode == "duplicate":
        state["duplicates"] = True
    elif mode == "foreign":
        state["receipt"]["metadata"]["agentseek_create_token"] = "other-token"  # noqa: S105 -- synthetic negative marker
    if mode == "one":
        assert call("delete") == {"status": "stopped"}
    else:
        with pytest.raises(ContractError):
            call("delete")
        assert not any(item[0] == "DELETE" for item in state["calls"])
    assert [item[0] for item in state["calls"]].count("POST") == 1
