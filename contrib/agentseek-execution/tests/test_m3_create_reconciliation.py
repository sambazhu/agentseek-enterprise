from dataclasses import asdict, replace

import pytest
from agentseek_execution import m3_create_reconciliation as module
from agentseek_execution.m3_create_fence import CreateFence
from agentseek_execution.m3_create_receipt import CreateBinding
from agentseek_execution.models import ContractError
from test_m3_platform_evidence import client_mock
from test_m3_platform_evidence import fixture as platform_fixture  # noqa: F401 -- shared synthetic HTTPS fixture


@pytest.fixture
def setup(request):
    config, _, _, _, reader, info = request.getfixturevalue("platform_fixture")
    directory = config.parent / "fence"
    directory.mkdir(mode=0o700)
    fence = CreateFence(directory)
    binding = CreateBinding(
        "run", "create", "tpl", "boot", *("a" * 64 for _ in range(3)), "0" * 64, "example.invalid", True
    )
    fence.claim(binding)
    return reader, info, fence, binding, directory / "create.pending"


@pytest.mark.parametrize(
    "remote_state,expected",
    [
        ("running", "exact_running_observed"),
        ("terminated", "exact_terminal_observed"),
        ("removed", "exact_terminal_observed"),
        ("paused", "exact_unresolved"),
        (None, "exact_unresolved"),
        ("unknown-future-state", "exact_unresolved"),
    ],
)
def test_exact_identity_never_grants_release_or_recovers_tokens(setup, monkeypatch, remote_state, expected):
    reader, info, fence, binding, path = setup
    info["state"] = remote_state
    before = path.read_bytes(), path.stat().st_mtime_ns
    calls = client_mock(monkeypatch, info)
    result = module.observe_pending(reader, fence, binding)
    assert result.state == expected and result.sandbox_id == "vm"
    assert not result.fence_release_allowed and not result.node_termination_proven and not result.aggregate_ready
    assert not any("token" in key or "metadata" in key for key in asdict(result))
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    assert [r.url.path for r in calls] == ["/sandboxes", "/sandboxes/vm"]
    with pytest.raises(ContractError):
        fence.claim(replace(binding, create_token="next"))


@pytest.mark.parametrize(
    "items,expected", [([], "empty_unresolved"), ([{"sandboxID": "vm"}, {"sandboxID": "other"}], "multiple_unresolved")]
)
def test_empty_or_multiple_does_not_select_target(setup, monkeypatch, items, expected):
    reader, info, fence, binding, _ = setup
    calls = client_mock(monkeypatch, info, items=items)
    result = module.observe_pending(reader, fence, binding)
    assert result.state == expected and result.sandbox_id is None and not result.fence_release_allowed
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("templateID", "other"),
        ("domain", "foreign.invalid"),
        ("metadata", {}),
        ("metadata", {"agentseek_run_id": "run", "agentseek_create_token": "other"}),
    ],
)
def test_partial_identity_is_conflict_not_candidate(setup, monkeypatch, field, value):
    reader, info, fence, binding, _ = setup
    info[field] = value
    client_mock(monkeypatch, info)
    result = module.observe_pending(reader, fence, binding)
    assert result.state == "identity_conflict" and result.sandbox_id is None


@pytest.mark.parametrize("items", [[{"sandboxID": "vm"}] * 2, [{"sandboxID": "../vm"}], [{}], {}])
def test_bad_inventory_denied_before_detail(setup, monkeypatch, items):
    reader, info, fence, binding, _ = setup
    calls = client_mock(monkeypatch, info, items=items)
    with pytest.raises(ContractError):
        module.observe_pending(reader, fence, binding)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 404, 500])
def test_http_failure_not_absence_or_termination(setup, monkeypatch, status):
    reader, info, fence, binding, _ = setup
    calls = client_mock(monkeypatch, info, status=status)
    with pytest.raises(ContractError):
        module.observe_pending(reader, fence, binding)
    assert len(calls) == 1


def test_wrong_original_plan_denied_before_network(setup, monkeypatch):
    reader, info, fence, binding, _ = setup
    calls = client_mock(monkeypatch, info)
    with pytest.raises(ContractError):
        module.observe_pending(reader, fence, replace(binding, create_token="replacement"))
    assert not calls


def test_fence_revalidated_after_network(setup, monkeypatch):
    reader, info, fence, binding, path = setup
    client_mock(monkeypatch, info)
    original = reader._get

    def get(*args):
        result = original(*args)
        path.write_bytes(b"partial")
        return result

    monkeypatch.setattr(reader, "_get", get)
    with pytest.raises(ContractError):
        module.observe_pending(reader, fence, binding)
