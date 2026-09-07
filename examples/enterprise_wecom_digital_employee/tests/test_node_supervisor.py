"""node_supervisor 定向测试：120s 硬截止、精确身份、幂等、宽限语义。"""

from __future__ import annotations

from pathlib import Path

from sandbox_poc.node_supervisor import (
    CONFIRM_WINDOW_SECONDS,
    DEADLINE_SECONDS,
    ManifestStore,
    NodeSupervisor,
    SandboxRecord,
)


class FakeClient:
    def __init__(self, records):
        self.records = {r.sandbox_id: r for r in records}
        self.kill_calls: list[str] = []

    def list(self):
        return list(self.records.values())

    def kill(self, sandbox_id):
        self.kill_calls.append(sandbox_id)
        self.records[sandbox_id] = SandboxRecord(
            sandbox_id=sandbox_id,
            template_alias=self.records[sandbox_id].template_alias,
            created_at=self.records[sandbox_id].created_at,
            status="terminating",
        )


def _manifest(tmp_path: Path, ids=("sbx-ours",)) -> ManifestStore:
    m = ManifestStore(tmp_path / "manifest.json")
    m.register_run("run-20260908-abc", "agentseek-m0-poc")
    for i in ids:
        m.register_sandbox(i)
    return m


def test_kill_issued_exactly_at_deadline_not_before(tmp_path):
    ours = SandboxRecord("sbx-ours", "agentseek-m0-poc", created_at=0.0, status="running")
    client = FakeClient([ours])
    sup = NodeSupervisor(client, _manifest(tmp_path))

    assert sup.poll_once(now=DEADLINE_SECONDS - 1) == []  # 截止前不动作
    assert sup.poll_once(now=DEADLINE_SECONDS) == ["sbx-ours"]  # 到时即终止
    assert client.kill_calls == ["sbx-ours"]


def test_only_registered_or_alias_matching_are_killed(tmp_path):
    ours = SandboxRecord("sbx-ours", "agentseek-m0-poc", 0.0, "running")
    unregistered_same_alias = SandboxRecord(
        "sbx-catchall", "agentseek-m0-poc", 0.0, "running"
    )
    other_alias = SandboxRecord("sbx-other", "other-template", 0.0, "running")
    no_alias = SandboxRecord("sbx-noalias", "", 0.0, "running")
    client = FakeClient([ours, unregistered_same_alias, other_alias, no_alias])
    sup = NodeSupervisor(client, _manifest(tmp_path))

    killed = sup.poll_once(now=DEADLINE_SECONDS + 5)
    # 精确 ID + alias catch-all；其他模板/无别名不动
    assert set(killed) == {"sbx-ours", "sbx-catchall"}
    assert "sbx-other" not in client.kill_calls
    assert "sbx-noalias" not in client.kill_calls


def test_kill_idempotent_and_confirmed_within_window(tmp_path):
    ours = SandboxRecord("sbx-ours", "agentseek-m0-poc", 0.0, "running")
    client = FakeClient([ours])
    sup = NodeSupervisor(client, _manifest(tmp_path))

    # 到时终止；状态变为 terminating（未确认）后，宽限窗口内每轮幂等重试
    assert sup.poll_once(now=DEADLINE_SECONDS) == ["sbx-ours"]
    assert sup.poll_once(now=DEADLINE_SECONDS + CONFIRM_WINDOW_SECONDS / 2) == [
        "sbx-ours"
    ]
    assert sup.poll_once(now=DEADLINE_SECONDS + 10) == ["sbx-ours"]
    # 确认终止（宽限窗口内）后不再重试
    client.records["sbx-ours"] = SandboxRecord(
        "sbx-ours", "agentseek-m0-poc", 0.0, "terminated"
    )
    assert sup.poll_once(now=DEADLINE_SECONDS + CONFIRM_WINDOW_SECONDS) == []
    assert client.kill_calls.count("sbx-ours") == 3


def test_no_kill_when_nothing_registered_or_alive(tmp_path):
    client = FakeClient(
        [
            SandboxRecord("sbx-old", "agentseek-m0-poc", -10000.0, "terminated"),
            SandboxRecord("sbx-foreign", "elsewhere", 0.0, "running"),
        ]
    )
    sup = NodeSupervisor(client, _manifest(tmp_path, ids=()))
    assert sup.poll_once(now=99999) == []
