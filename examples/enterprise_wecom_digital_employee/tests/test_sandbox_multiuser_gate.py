"""I07: real SQLite gates and in-process broker; no external execution."""

# ruff: noqa: F811
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier, Event

import pytest
from agentseek_execution.business_broker import BusinessBroker
from agentseek_execution.csv_business import BusinessStore
from test_sandbox_remote import remote  # noqa: F401


@pytest.mark.parametrize("side", ["gateway", "node"])
@pytest.mark.parametrize("state", ["reserved", "reconciling"])
def test_foreign_unresolved_row_has_side_specific_effect(remote, side, state):
    s = remote
    foreign = replace(s.request, owner_id="other-owner", request_id="other-request")
    store = getattr(s, side)
    attempt = store.reserve(foreign)
    if state == "reconciling":
        store.record(attempt, state, None)
    assert s.runner.grant_unused(s.owner, s.request.request_id) is (side == "node")

    if side == "gateway":
        with pytest.raises(ValueError, match="unresolved attempt"):
            s.runner.execute(s.request, s.data)
        assert s.gateway.snapshot(s.request) is None
        assert s.calls == []
    else:
        # Advisory mirror availability is not a distributed reservation.
        result = s.runner.execute(s.request, s.data)
        assert result.state == "reconciling" and not result.cleanup_confirmed
        assert s.runner.execute(s.request, s.data) == result
        assert s.calls == ["execute"]
        with pytest.raises(ValueError):
            s.runner.recover_request(s.request)
        assert s.calls == ["execute", "result"]
        assert s.gateway.snapshot(s.request) == result
        assert not s.runner.grant_unused(s.owner, s.request.request_id)
    assert s.node.snapshot(s.request) is None
    assert store.snapshot(foreign).state == "reconciling"
    assert s.events == []


@pytest.mark.parametrize("side", ["gateway", "node"])
@pytest.mark.parametrize("state", ["failed", "succeeded"])
def test_foreign_terminal_row_releases_capacity_not_request_identity(remote, side, state):
    s = remote
    foreign = replace(s.request, owner_id="other-owner", request_id="other-request")
    store = getattr(s, side)
    attempt = store.reserve(foreign)
    artifact = store.persist(attempt, foreign.owner_id, s.output) if state == "succeeded" else None
    store.record(attempt, state, artifact)
    with pytest.raises(ValueError, match="already exists"):
        store.reserve(foreign)
    assert s.runner.grant_unused(s.owner, s.request.request_id)
    assert s.runner.execute(s.request, s.data).state == "succeeded"
    assert s.events == ["create", "execute", "destroy"]
    assert store.snapshot(foreign).state == state


@pytest.mark.parametrize("same_request", [False, True])
def test_independent_connections_compete_for_one_whole_store_slot(remote, same_request):
    s = remote
    stores = [BusinessStore(s.gateway.path.parent) for _ in range(8)]
    requests = [s.request if same_request else replace(
        s.request, owner_id=f"owner-{i}", request_id=f"request-{i}") for i in range(8)]
    barrier = Barrier(8)

    def reserve(index):
        barrier.wait(timeout=10)
        try:
            return stores[index].reserve(requests[index])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))
    assert sum(result is not None for result in results) == 1
    with s.gateway._connect() as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone() == (1,)
    assert s.events == [] and s.calls == []
    # Only a shared SQLite store is covered, not distributed locks or VM quota.


def test_stale_mirror_blocks_other_owner_until_readonly_reconciliation(remote):
    s = remote
    foreign = replace(s.request, owner_id="other-owner", request_id="other-request")
    s.client.lose = True
    assert s.runner.execute(s.request, s.data).state == "reconciling"
    assert s.node.snapshot(s.request).state == "succeeded"
    assert not s.runner.grant_unused(foreign.owner_id, foreign.request_id)
    with pytest.raises(ValueError, match="unresolved attempt"):
        s.gateway.reserve(foreign)
    assert s.runner.recover_request(s.request).state == "succeeded"
    assert s.runner.grant_unused(foreign.owner_id, foreign.request_id)
    assert not s.runner.grant_unused(s.owner, s.request.request_id)
    assert s.calls == ["execute", "result"]
    assert s.events == ["create", "execute", "destroy"]


def test_advisory_success_can_become_stale_before_atomic_reserve(remote):
    s = remote
    assert s.runner.grant_unused(s.owner, s.request.request_id)
    foreign = replace(s.request, owner_id="other-owner", request_id="other-request")
    s.gateway.reserve(foreign)
    with pytest.raises(ValueError, match="unresolved attempt"):
        s.runner.execute(s.request, s.data)
    assert s.gateway.snapshot(s.request) is None
    assert s.node.snapshot(s.request) is None
    assert s.calls == [] and s.events == []


def test_two_valid_permits_compete_before_provider_creation(remote):
    s = remote
    other = replace(s.request, owner_id="other-owner", request_id="other-request")
    permit = next(iter(s.broker.permits.values()))
    entered, release = Event(), Event()

    def provider_for(selected):
        provider = s.broker.provider_for(selected)
        # Both requests have valid synthetic permits and input. This test
        # exercises the store gate, not rejection by the fixture validator.
        provider.validate_request = lambda request, data: None
        original_create = provider.create

        def create(attempt):
            original_create(attempt)
            entered.set()
            assert release.wait(timeout=10)

        provider.create = create
        return provider

    broker = BusinessBroker(permits=[permit, replace(permit, request=other)],
                            store=s.node, workspace=s.broker.workspace,
                            provider_for=provider_for)

    def execute(request):
        return broker.dispatch("Bearer " + s.token, {
            "operation": "execute", "request": asdict(request),
            "input": base64.b64encode(s.data).decode(),
        })

    with ThreadPoolExecutor(max_workers=1) as executor:
        winner = executor.submit(execute, s.request)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(ValueError, match="unresolved attempt"):
                execute(other)
            assert s.node.snapshot(other) is None
            assert s.events == ["create"]
        finally:
            release.set()
        result = winner.result(timeout=10)
    assert result["outcome"]["state"] == "succeeded"
    assert execute(s.request) == result
    assert s.events == ["create", "execute", "destroy"]
    assert s.node.snapshot(other) is None
