import hashlib
import json
import struct

import httpx
import pytest
from agentseek_execution import m3_auth_probe as probe
from agentseek_execution.models import ContractError


def request(**changes):
    values = {
        "endpoint": "E1",
        "host": "49983-vm.example.invalid",
        "proxy_port": 13080,
        "credentials": probe.Credentials("synthetic-key", "synthetic-traffic", "synthetic-envd"),
        "layer": "traffic",
        "state": "correct",
    }
    values.update(changes)
    return probe.build_request(**values)


@pytest.mark.parametrize("endpoint", ["E1", "E2", "E3"])
@pytest.mark.parametrize("layer", ["traffic", "envd"])
@pytest.mark.parametrize("state", ["correct", "missing", "wrong", "cross_guest"])
def test_single_variable_matrix(endpoint, layer, state):
    original = request(endpoint=endpoint, layer=layer)
    candidate = request(
        endpoint=endpoint,
        layer=layer,
        state=state,
        replacement="synthetic-replacement" if state in {"wrong", "cross_guest"} else None,
    )
    expected = dict(original.headers)
    target = probe.TOKEN_HEADERS[layer]
    if state == "missing":
        del expected[target]
    elif state != "correct":
        expected[target] = "synthetic-replacement"
    assert candidate.headers == expected
    assert (candidate.method, candidate.url, candidate.body) == (original.method, original.url, original.body)
    assert "synthetic" not in repr(candidate)


def test_exact_protocol_and_fixed_command():
    command = request()
    assert command.body[0] == 0
    assert struct.unpack(">I", command.body[1:5])[0] == len(command.body[5:])
    assert json.loads(command.body[5:]) == {
        "process": {"cmd": "/bin/bash", "args": ["-l", "-c", probe.MARKER_COMMAND], "envs": {}},
        "stdin": False,
    }
    assert command.headers["Authorization"] == "Basic cm9vdDo="
    assert "Authorization" not in request(endpoint="E3").headers
    assert request(endpoint="E3").body == b'{"path":"/etc/hostname"}'
    assert request(endpoint="E2").url.endswith("/files?path=%2Fetc%2Fhostname&username=root")


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "evil.invalid/path"},
        {"host": "bad\r\nHost:x"},
        {"proxy_port": True},
        {"endpoint": "Delete"},
        {"layer": "api_key"},
        {"state": "guess"},
        {"state": "missing", "replacement": "unused"},
        {"state": "wrong", "replacement": "synthetic-traffic"},
        {"state": "wrong", "replacement": "bad\nheader"},
        {"layer": "envd", "credentials": probe.Credentials("synthetic-key", "synthetic-traffic", None)},
    ],
)
def test_invalid_or_unavailable_credentials_blocked(changes):
    with pytest.raises(ContractError):
        request(**changes)


@pytest.mark.parametrize("endpoint", ["E1", "E2", "E3"])
@pytest.mark.parametrize("alias", ["e2b", "cube"])
@pytest.mark.parametrize("state", ["correct", "missing", "wrong", "cross_guest"])
def test_proxy_alias_four_states_without_envd(endpoint, alias, state):
    built = request(
        endpoint=endpoint,
        traffic_alias=alias,
        state=state,
        credentials=probe.Credentials("synthetic-key", "synthetic-traffic", None),
        replacement="synthetic-replacement" if state in {"wrong", "cross_guest"} else None,
    )
    assert "X-Access-Token" not in built.headers
    selected = probe.TRAFFIC_ALIASES[alias]
    other = probe.TRAFFIC_ALIASES["cube" if alias == "e2b" else "e2b"]
    assert other not in built.headers
    if state == "missing":
        assert selected not in built.headers
    else:
        assert built.headers[selected] == ("synthetic-traffic" if state == "correct" else "synthetic-replacement")
    assert built.headers["X-API-Key"] == "synthetic-key"


@pytest.mark.parametrize("alias", ["both", "X-Access-Token", "", None, "cube\r\n"])
def test_unapproved_alias_rejected(alias):
    with pytest.raises(ContractError):
        request(traffic_alias=alias)


@pytest.mark.parametrize("state", ["correct", "missing", "wrong", "cross_guest"])
def test_envd_unavailable_never_invents_reference(state):
    with pytest.raises(ContractError):
        request(layer="envd", state=state, credentials=probe.Credentials("synthetic-key", "traffic", None))


class Raw(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def response_observation(status=200, chunks=(b"secret-body",), headers=None):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, headers=headers, stream=Raw(chunks))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe._observe(client, request(state="missing"))
    assert len(calls) == 1
    assert "e2b-traffic-access-token" not in calls[0].headers
    assert "secret-body" not in repr(result)
    return result


@pytest.mark.parametrize("status", [200, 302, 401, 403, 404, 500])
def test_status_is_observation_not_auth_pass(status):
    result = response_observation(status)
    assert result.status == status and result.complete and result.reason == "response"
    assert result.body_size == 11 and result.body_sha256 == hashlib.sha256(b"secret-body").hexdigest()
    assert result.protocol.kind in {"unknown", "http_other", "http_denial_signal"}


@pytest.mark.parametrize(
    "headers,reason",
    [
        ({"Content-Encoding": "gzip"}, "encoding"),
        ({"Content-Length": "65537"}, "length"),
        ({"Content-Length": "-1"}, "length"),
        ({"Content-Length": "10"}, "length_mismatch"),
    ],
)
def test_invalid_response_not_complete(headers, reason):
    result = response_observation(headers=headers)
    assert not result.complete and result.reason == reason and result.body_sha256 is None


def test_stream_limit():
    result = response_observation(chunks=(b"x" * 4096,) * 17)
    assert result.reason == "body_limit" and result.body_size == 65536


def test_deadline(monkeypatch):
    ticks = iter([0, 6])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    assert response_observation().reason == "deadline"


def test_transport_error_not_leaked_or_retried():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectError("synthetic-key secret-body")  # noqa: TRY003 -- secret-redaction fixture

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe._observe(client, request())
    assert len(calls) == 1
    assert result == probe.Observation(None, 0, None, False, "transport_error")


@pytest.mark.parametrize("budget", [0, 9.9, float("nan"), float("inf"), True])
def test_insufficient_budget_before_client(monkeypatch, budget):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("client constructed"))
    with pytest.raises(ContractError):
        probe.observe(request(), remaining_seconds=budget)
