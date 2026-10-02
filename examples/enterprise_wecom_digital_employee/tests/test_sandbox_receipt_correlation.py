"""Two-user audit correlation with real stores, capabilities and mock HTTP only."""

# ruff: noqa: F811
import asyncio
import hashlib
import json
import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentseek_execution.business_execution import BusinessRequest
from agentseek_execution.csv_business import BusinessStore
from agentseek_files.models import FileScope
from agentseek_wecom.file_delivery import STATE_KEY, file_ref, resolve_capability
from enterprise_wecom_digital_employee.native_file_delivery import native_file_tools
from enterprise_wecom_digital_employee.sandbox_authorization import (
    SandboxGrant,
    instruction_digest,
    runtime_scope,
    scoped_owner,
)
from enterprise_wecom_digital_employee.sandbox_remote import RemoteCsvRunner, remote_csv_tools
from enterprise_wecom_digital_employee.sandbox_workspace_binding import WorkspaceBindings
from enterprise_wecom_digital_employee.tool_observation import identifier_hash, observed, scope_hash
from loguru import logger
from test_native_file_delivery import rig  # noqa: F401 - dependency of pair fixture
from test_sandbox_multiuser_integration import pair  # noqa: F401
from test_sandbox_remote import remote  # noqa: F401


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def audit_lines():
    lines = []
    sink = logger.add(lambda m: lines.append(m.record.copy()), level="INFO", format="{message}",
        filter=lambda r: r["message"].startswith(("sandbox_tool ", "workspace_delivery_tool ")))
    try:
        yield lines
    finally:
        logger.remove(sink)


def decoded(lines):
    return [dict(part.split("=", 1) for part in line["message"].split()[1:]) for line in lines]


def assert_pairs(lines):
    events = decoded(lines)
    for event in {r["event"] for r in events}:
        rows = [r for r in events if r["event"] == event]
        assert len(rows) == 2 and rows[0]["phase"] == "start"
        assert rows[1]["phase"] in {"complete", "failed", "cancelled"}
        assert rows[0]["scope_sha256"] == rows[1]["scope_sha256"]
        assert rows[0]["call_sha256"] == rows[1]["call_sha256"]
    assert all(r["level"].name == "INFO" and r["exception"] is None for r in lines)
    return [r for r in events if r["phase"] == "complete"]


async def prepared(pair, tmp_path):
    seen = await pair.receive([pair.message(user) for user in pair.records])
    directory = (tmp_path / "mirror").resolve()
    directory.mkdir(mode=0o700)
    store = BusinessStore(directory)
    class NoNetwork:
        def exchange(self, *args, **kwargs):
            pytest.fail("terminal query must not contact broker")
    runner = RemoteCsvRunner(client=NoNetwork(), store=store)
    requests, outcomes, grants = {}, {}, {}
    for user, record in pair.records.items():
        runtime = seen["wecom:" + user]
        scope = runtime_scope(runtime)
        inbound = pair.store.store_bytes(scope=FileScope(*scope), filename="private.csv",
            data=b"group,amount\nA,1\n")
        runtime.state["current_files"] = [inbound.to_dict()]
        runtime.tool_call_id = "same-model-call-id"  # Deliberately collide across users.
        request = BusinessRequest("private-request-" + user, scoped_owner(scope), inbound.file_id, "private instruction")
        attempt = store.reserve(request)
        artifact = store.persist(attempt, request.owner_id, pair.store.original_path(record).read_bytes())
        store.record(attempt, "succeeded", artifact)
        outcome = store.snapshot(request)
        WorkspaceBindings(runner, pair.store).publish(scope, request, outcome)
        requests[user], outcomes[user] = request, outcome
        grants[scope] = SandboxGrant(request.request_id, *scope, inbound.file_id,
            instruction_digest(request.instruction), time.time() + 120)
    tools = remote_csv_tools(grant_for=lambda rt: grants[runtime_scope(rt)], file_store=pair.store, runner=runner)
    return seen, requests, outcomes, tools, runner


def test_two_users_task_file_receipt_logs_join_without_identity_leak(pair, tmp_path, audit_lines):
    async def run():
        seen, requests, outcomes, tools, _ = await prepared(pair, tmp_path)
        users = list(pair.records)
        queried = await asyncio.gather(*(tools[1].coroutine(seen["wecom:" + u]) for u in users))
        delivered = await asyncio.gather(*(native_file_tools()[1].coroutine(
            file_ref(pair.records[u]), seen["wecom:" + u]) for u in users))
        ends = assert_pairs(audit_lines)
        assert len(ends) == 4 and len({r["scope_sha256"] for r in ends}) == 2
        assert len({r["call_sha256"] for r in ends}) == 1  # call ID alone is not a join key.
        for user, query, receipt in zip(users, queried, delivered, strict=True):
            runtime = seen["wecom:" + user]
            rows = [r for r in ends if r["scope_sha256"] == scope_hash(runtime)]
            q = next(r for r in rows if r["name"] == "get_sandbox_task_result")
            d = next(r for r in rows if r["name"] == "deliver_workspace_file")
            assert q["request_sha256"] == digest(requests[user].request_id)
            assert q["attempt_sha256"] == digest(outcomes[user].attempt)
            assert q["artifact_sha256"] == digest(outcomes[user].artifact_ref)
            assert q["file_ref_sha256"] == d["file_ref_sha256"] == digest(file_ref(pair.records[user]))
            assert d["delivery_sha256"] == digest(receipt["delivery_id"]) and d["reused"] == "False"
            assert d["state"] == "api_accepted" and not receipt["user_receipt_confirmed"]
            assert d["request_sha256"] == d["attempt_sha256"] == "absent"  # No invented task causality.
            capability = resolve_capability(runtime.state[STATE_KEY], runtime_scope(runtime))
            with capability.ledger.connect() as db:
                assert db.execute("SELECT status FROM deliveries WHERE id=?", (receipt["delivery_id"],)).fetchone() == ("api_accepted",)
            assert query["workspace"]["association"]["artifact_ref"] == outcomes[user].artifact_ref
        text = "\n".join(r["message"] for r in audit_lines)
        for secret in [str(tmp_path), "alice", "bob", "private instruction", "same-model-call-id", "SECRET",
                       *(r.request_id for r in requests.values()), *(o.attempt for o in outcomes.values()),
                       *(r.relative_dir for r in pair.records.values())]:
            assert secret not in text
        assert len(pair.calls) == 4
    asyncio.run(run())


def test_shared_file_has_multiple_task_edges_not_multiple_sends(pair, tmp_path, audit_lines):
    async def run():
        seen, requests, outcomes, tools, runner = await prepared(pair, tmp_path)
        runtime = seen["wecom:alice"]
        scope = runtime_scope(runtime)
        first = await tools[1].coroutine(runtime)
        deliver = native_file_tools()[1]
        ref = file_ref(pair.records["alice"])
        await deliver.coroutine(ref, runtime)
        request = replace(requests["alice"], request_id="second-independent-task")
        attempt = runner.store.reserve(request)
        artifact = runner.store.persist(attempt, request.owner_id, pair.store.original_path(pair.records["alice"]).read_bytes())
        runner.store.record(attempt, "succeeded", artifact)
        WorkspaceBindings(runner, pair.store).publish(scope, request, runner.store.snapshot(request))
        grant = SandboxGrant(request.request_id, *scope, request.input_ref, instruction_digest(request.instruction), time.time() + 120)
        current = remote_csv_tools(grant_for=lambda _: grant, file_store=pair.store, runner=runner)
        second = await current[1].coroutine(runtime)
        repeated = await deliver.coroutine(ref, runtime)
        rows = assert_pairs(audit_lines)
        queries = [r for r in rows if r["name"] == "get_sandbox_task_result"]
        sends = [r for r in rows if r["name"] == "deliver_workspace_file"]
        assert len(queries) == len(sends) == 2
        assert queries[0]["attempt_sha256"] != queries[1]["attempt_sha256"]
        assert queries[0]["artifact_sha256"] != queries[1]["artifact_sha256"]
        assert len({r["file_ref_sha256"] for r in rows}) == 1
        assert sends[0]["delivery_sha256"] == sends[1]["delivery_sha256"]
        assert sends[1]["reused"] == "True" and repeated["reused_receipt"]
        assert all(r["request_sha256"] == "absent" for r in sends)
        assert first["attempt"] == outcomes["alice"].attempt and second["attempt"] == attempt
        assert len(pair.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [False, True])
def test_receipt_reuse_and_explicit_resend_have_distinct_events(pair, audit_lines, timeout):
    async def run():
        if timeout:
            pair.mode["timeout"] = "alice"
        seen = await pair.receive([pair.message("alice")])
        runtime = seen["wecom:alice"]
        deliver = native_file_tools()[1]
        ref = file_ref(pair.records["alice"])
        first = await deliver.coroutine(ref, runtime)
        duplicate = await deliver.coroutine(ref, runtime)
        new = await pair.receive([pair.message("alice", text="把 summary.csv 再发给我", message_id="new-explicit-id")])
        resent = await deliver.coroutine(ref, new["wecom:alice"])
        rows = assert_pairs(audit_lines)
        assert len(rows) == 3 and len({r["event"] for r in rows}) == 3
        assert rows[0]["delivery_sha256"] == rows[1]["delivery_sha256"] == digest(first["delivery_id"])
        assert rows[2]["delivery_sha256"] == digest(resent["delivery_id"]) != rows[0]["delivery_sha256"]
        assert [r["reused"] for r in rows] == ["False", "True", "False"]
        assert all(r["state"] == ("uncertain" if timeout else "api_accepted") for r in rows)
        assert duplicate["reused_receipt"] and len(pair.calls) == 4
    asyncio.run(run())


def test_cross_user_denial_does_not_log_foreign_association(pair, audit_lines):
    async def run():
        seen = await pair.receive([pair.message("bob")])
        runtime = seen["wecom:bob"]
        foreign = file_ref(pair.records["alice"])
        result = await native_file_tools()[1].coroutine(foreign, runtime)
        end, = assert_pairs(audit_lines)
        assert result["status"] == end["state"] == "denied"
        assert end["file_ref_sha256"] == end["delivery_sha256"] == "absent"
        assert digest(foreign) not in json.dumps(end) and pair.calls == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancelled", "fatal"])
def test_delivery_cancellation_or_fatal_keeps_terminal_event_no_receipt(pair, audit_lines, failure):
    class Fatal(BaseException):
        pass
    error = asyncio.CancelledError if failure == "cancelled" else Fatal
    async def run():
        seen = await pair.receive([pair.message("alice")])
        runtime = seen["wecom:alice"]
        capability = resolve_capability(runtime.state[STATE_KEY], runtime_scope(runtime))
        async def fail(**kwargs):
            raise error("PRIVATE exception body")
        capability.upload = fail
        with pytest.raises(error):
            await native_file_tools()[1].coroutine(file_ref(pair.records["alice"]), runtime)
        assert_pairs(audit_lines)
        terminal = decoded(audit_lines)[-1]
        assert terminal["phase"] == ("cancelled" if failure == "cancelled" else "failed")
        assert "delivery_sha256" not in terminal and "PRIVATE" not in str(audit_lines)
        assert pair.calls == []
    asyncio.run(run())


def test_sink_failure_cannot_change_success_or_trigger_duplicate_send(pair, monkeypatch):
    async def run():
        seen = await pair.receive([pair.message("alice")])
        runtime = seen["wecom:alice"]
        def broken(*args, **kwargs):
            raise RuntimeError("PRIVATE log sink")
        monkeypatch.setattr(logger, "info", broken)
        deliver = native_file_tools()[1]
        first = await deliver.coroutine(file_ref(pair.records["alice"]), runtime)
        again = await deliver.coroutine(file_ref(pair.records["alice"]), runtime)
        assert first["status"] == "api_accepted" and again["reused_receipt"]
        assert len(pair.calls) == 2
    asyncio.run(run())


def test_observation_extraction_failure_never_changes_sent_result(pair, monkeypatch, audit_lines):
    import enterprise_wecom_digital_employee.tool_observation as observation
    async def run():
        seen = await pair.receive([pair.message("alice")])
        def fail(*args, **kwargs):
            raise ValueError("PRIVATE diagnostic failure")
        monkeypatch.setattr(observation, "_fields", fail)
        result = await native_file_tools()[1].coroutine(file_ref(pair.records["alice"]), seen["wecom:alice"])
        assert result["status"] == "api_accepted" and len(pair.calls) == 2
        end, = assert_pairs(audit_lines)
        assert end["state"] == "other" and "PRIVATE" not in str(audit_lines)
    asyncio.run(run())


def test_upload_failure_is_correlated_without_send_or_exception_body(pair, audit_lines):
    async def run():
        seen = await pair.receive([pair.message("alice")])
        runtime = seen["wecom:alice"]
        capability = resolve_capability(runtime.state[STATE_KEY], runtime_scope(runtime))
        async def fail(**kwargs):
            raise ValueError("PRIVATE upstream credential body")
        capability.upload = fail
        result = await native_file_tools()[1].coroutine(file_ref(pair.records["alice"]), runtime)
        end, = assert_pairs(audit_lines)
        assert result["status"] == end["state"] == "upload_failed"
        assert end["delivery_sha256"] == digest(result["delivery_id"])
        assert not result["api_accepted"] and pair.calls == []
        assert "PRIVATE" not in str(audit_lines)
    asyncio.run(run())


def test_observation_does_not_add_model_identity_or_correlation_parameters(remote):
    tools = remote_csv_tools(grant_for=lambda _: remote.grant, file_store=remote.files, runner=remote.runner)
    expected = [{"input_ref", "instruction"}, {"input_ref", "instruction"}, {"artifact_ref"}, {"file_ref"}]
    for item, fields in zip([*tools, native_file_tools()[1]], expected, strict=True):
        assert set(item.tool_call_schema.model_json_schema()["properties"]) == fields


@pytest.mark.parametrize("value", [None, [], "", "x" * 4097, "\ud800"])
def test_identifier_hash_rejects_invalid_or_unbounded_values(value):
    assert identifier_hash(value) == "absent"


def test_observer_whitelist_omits_untrusted_payload_and_state_identity(audit_lines):
    runtime = SimpleNamespace(context={}, state={"enterprise": {"tenant_key": "SECRET"}}, tool_call_id="call\nSECRET")
    @observed
    async def get_sandbox_task_result(runtime):
        return {"state": "SECRET invalid state", "body": "SECRET body", "request_id": "req\nSECRET",
                "attempt": ["SECRET"], "artifact_ref": None,
                "workspace": {"association": {"state": "verified", "file_ref": "SECRET"}}}
    result = asyncio.run(get_sandbox_task_result(runtime))
    assert result["body"] == "SECRET body"  # Observation does not modify tool semantics.
    end, = assert_pairs(audit_lines)
    assert end["state"] == "other" and end["scope_sha256"] == "absent"
    assert end["attempt_sha256"] == end["file_ref_sha256"] == "absent"
    assert end["request_sha256"] == digest("req\nSECRET")
    for row in audit_lines:
        assert "SECRET" not in row["message"] and "\n" not in row["message"]
        assert len(row["message"]) < 1400
    assert set(end) == {"schema", "name", "event", "phase", "state", "current", "request_sha256",
        "attempt_sha256", "artifact_sha256", "file_ref_sha256", "delivery_sha256", "reused",
        "call_sha256", "scope_sha256"}


def test_run_and_read_event_hashes_match_actual_attempt(remote, audit_lines):
    tools = remote_csv_tools(grant_for=lambda _: remote.grant, file_store=remote.files, runner=remote.runner)
    async def run():
        out = await tools[0].coroutine(remote.record.file_id, remote.request.instruction, remote.runtime)
        await tools[2].coroutine(out["artifact_ref"], remote.runtime)
        rows = assert_pairs(audit_lines)
        assert len(rows) == 2
        for row in rows:
            assert row["attempt_sha256"] == digest(out["attempt"])
            assert row["artifact_sha256"] == digest(out["artifact_ref"])
        assert re.fullmatch(r"[0-9a-f]{64}", rows[0]["file_ref_sha256"])
        assert rows[1]["file_ref_sha256"] == "absent"  # Artifact read is not workspace proof.
        assert remote.events == ["create", "execute", "destroy"]
    asyncio.run(run())
