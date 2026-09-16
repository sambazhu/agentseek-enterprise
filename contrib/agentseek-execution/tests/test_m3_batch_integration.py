"""Local batch composition: real journals/approvals, synthetic network/closeout."""

import hashlib
import json
import time
from dataclasses import asdict, replace

import httpx
import pytest
from agentseek_execution import m3_create_worker as module
from agentseek_execution.m3_batch_quota import BatchQuota
from agentseek_execution.m3_closeout_evidence import CloseoutEvidence
from agentseek_execution.models import ContractError, canonical
from test_m3_create_worker import setup as worker_setup  # noqa: F401 -- shared private fixture


@pytest.mark.parametrize("size", [2, 4])
def test_complete_batch_reopens_journals_and_rejects_all_replays(request, monkeypatch, size):
    s = request.getfixturevalue("worker_setup")
    requests = [json.dumps({
        "templateID": "tpl",
        "metadata": {"agentseek_run_id": "run", "agentseek_create_token": f"slot-{i}"},
        "network": {"allowPublicTraffic": i >= 2},
    }).encode() for i in range(size)]
    sequence = tuple(replace(s.plan, create_token=f"slot-{i}", restricted=i < 2,
                             request_sha256=hashlib.sha256(raw).hexdigest()) for i, raw in enumerate(requests))
    quota_path = s.root / "quota"
    quota_path.mkdir(mode=0o700)
    sent = []

    def handle(request):
        index = len(sent)
        assert request.method == "POST" and request.content == requests[index]
        assert (quota_path / f"create-slot-{index}").is_file()
        sent.append(request.content)
        return httpx.Response(201, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(
            json.dumps({**s.state["response"], "sandboxID": f"vm-{index}"}).encode()))

    monkeypatch.setattr(httpx, "Client", lambda **kw: httpx._client.Client(transport=httpx.MockTransport(handle)))
    previous = None
    attempts = []
    for index, plan in enumerate(sequence):
        approval = s.root / f"approval-{index}"
        raw = json.dumps({**s.approval, "plan": asdict(plan), "expires_epoch": time.time() + 100}).encode()
        approval.write_bytes(raw)
        approval.chmod(0o600)
        kwargs = {**s.kwargs, "plan": plan, "approval_path": approval,
                  "approval_digest": hashlib.sha256(raw).hexdigest(),
                  "batch_quota": BatchQuota(quota_path, sequence)}
        if previous is not None:
            old = replace(previous, intent_sha256="0" * 64)
            digest = hashlib.sha256(canonical(asdict(old)).encode()).hexdigest()

            def closeout(digest=digest, prior_index=index - 1):
                return CloseoutEvidence(f"vm-{prior_index}", "known_target_absent", time.monotonic(), 42, 1, digest)

            kwargs.update(previous_create=previous, collect_closeout=closeout)
        previous = module.CreateWorker(**kwargs).create(requests[index])
        assert s.vault.read(previous).sandbox_id == f"vm-{index}"
        s.kwargs["fence"].verify_pending(replace(previous, intent_sha256="0" * 64))
        attempts.append(kwargs)
    assert sent == requests
    assert len(list(quota_path.iterdir())) == size
    assert len(list((s.root / "create-fence").glob("create.closed-*"))) == size - 1
    for index, kwargs in enumerate(attempts):
        kwargs["batch_quota"] = BatchQuota(quota_path, sequence)
        with pytest.raises(ContractError):
            module.CreateWorker(**kwargs).create(requests[index])
    assert sent == requests
    assert len(list(s.directory.glob("*.receipt"))) == size
