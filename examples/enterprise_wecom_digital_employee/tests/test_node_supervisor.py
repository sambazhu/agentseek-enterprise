"""node_supervisor 定向测试（v3：P1×4/P2 修复的故障路径全覆盖）。"""

from __future__ import annotations

import json
import threading

import pytest
from sandbox_poc.node_supervisor import (
    CONFIRM_WINDOW_SECONDS,
    DEADLINE_SECONDS,
    ManifestError,
    ManifestStore,
    NodeSupervisor,
    NotFoundError,
    SandboxRecord,
    check_gate,
)

RUN = "run-20260908-abc"
ALIAS = "agentseek-m0-poc"
TPL = "tpl-m0-poc"


class Clocks:
    """可注入双时钟：epoch 可回拨，monotonic 单调。"""

    def __init__(self, epoch: float = 1000.0, mono: float = 0.0) -> None:
        self.epoch = epoch
        self.mono = mono

    def epoch_now(self) -> float:
        return self.epoch

    def mono_now(self) -> float:
        return self.mono


class FakeClient:
    def __init__(self, listing, *, hydrate_map=None, kill_mode="ok"):
        self.records = {r.sandbox_id: r for r in listing}
        self.hydrate_map = hydrate_map or {}
        self.kill_mode = kill_mode
        self.kill_calls: list[str] = []
        self.list_failures = 0
        self.hydrate_failures: set[str] = set()

    def list(self):
        if self.list_failures > 0:
            self.list_failures -= 1
            raise ConnectionError("control plane down")
        return list(self.records.values())

    def hydrate(self, record):
        if record.sandbox_id in self.hydrate_failures:
            raise RuntimeError("info timeout")
        extra = self.hydrate_map.get(record.sandbox_id, {})
        return SandboxRecord(
            sandbox_id=record.sandbox_id,
            template_id=record.template_id,
            state=record.state,
            started_at=extra.get("started_at", record.started_at),
            run_marker=extra.get("run_marker", record.run_marker),
        )

    def kill(self, sandbox_id):
        self.kill_calls.append(sandbox_id)
        if self.kill_mode == "notfound":
            raise NotFoundError(sandbox_id)
        if self.kill_mode == "error":
            raise RuntimeError("kill transport error")


@pytest.fixture
def manifest(tmp_path) -> ManifestStore:
    m = ManifestStore(tmp_path / "manifest.json")
    m.register_run(RUN, ALIAS, TPL, started_at=1000.0)
    return m


def _sup(client, manifest, tmp_path, clocks=None):
    clocks = clocks or Clocks()
    return (
        NodeSupervisor(
            client,
            manifest,
            alarm_file=tmp_path / "alarm",
            epoch_clock=clocks.epoch_now,
            mono_clock=clocks.mono_now,
        ),
        clocks,
    )


def _rec(sid, tpl=TPL, state="running", started=None, marker=None):
    return SandboxRecord(sid, tpl, state, started_at=started, run_marker=marker)


# ---------- P1-1：终止授权仅两级（marker / 登记），无模板兜底 ----------

def test_marker_ours_killed_other_run_marker_excluded(tmp_path, manifest):
    client = FakeClient(
        [_rec("sbx-ours"), _rec("sbx-other")],
        hydrate_map={
            "sbx-ours": {"started_at": 1000.0, "run_marker": RUN},
            "sbx-other": {"started_at": 1000.0, "run_marker": "run-OTHER"},
        },
    )
    sup, clocks = _sup(client, manifest, tmp_path)
    sup.poll_once()  # 首次观测（mono=0）锚定 deadline_mono=120
    clocks.mono = DEADLINE_SECONDS
    assert sup.poll_once() == ["sbx-ours"]
    assert client.kill_calls == ["sbx-ours"]


def test_same_template_markerless_unregistered_NEVER_killed(tmp_path, manifest):
    """P1-1 负向：同模板、无 marker、未登记 → 只告警，绝不删除。"""
    client = FakeClient(
        [_rec("sbx-stranger", started=None)],
        hydrate_map={"sbx-stranger": {"started_at": 1000.0}},  # 同模板无标记
    )
    sup, clocks = _sup(client, manifest, tmp_path)
    clocks.mono = 10_000.0  # 远超任何截止
    assert sup.poll_once() == []
    assert client.kill_calls == []
    assert (tmp_path / "alarm").exists()  # 告警阻断新建


def test_registered_id_killed_via_registered_at(tmp_path, manifest):
    manifest.register_sandbox("sbx-reg")
    reg_at = manifest.registered_at("sbx-reg")
    client = FakeClient([_rec("sbx-reg")], hydrate_map={"sbx-reg": {}})  # 无 marker
    sup, clocks = _sup(client, manifest, tmp_path)
    clocks.epoch = reg_at  # 首观时墙钟对齐登记时刻 → deadline_mono=120
    sup.poll_once()  # 观测锚定（start=registered_at）
    clocks.mono = DEADLINE_SECONDS + 1
    assert sup.poll_once() == ["sbx-reg"]


# ---------- P2：单调钟 deadline，墙钟回拨不延长 ----------

def test_wall_clock_backward_jump_does_not_extend(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})
    sup, clocks = _sup(client, manifest, tmp_path)
    sup.poll_once()  # 首观：epoch=1000, mono=0 → deadline_mono=120
    clocks.mono, clocks.epoch = 60.0, 1060.0
    assert sup.poll_once() == []
    # 墙钟回拨到 1021（epoch 年龄仅 21s）但 mono=121 已过截止 → 仍须终止
    clocks.mono, clocks.epoch = 121.0, 1021.0
    assert sup.poll_once() == ["sbx-a"]


def test_deadline_not_fired_before_mono_deadline(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})
    sup, clocks = _sup(client, manifest, tmp_path)
    clocks.mono = DEADLINE_SECONDS - 1
    assert sup.poll_once() == []


# ---------- P1-3：告警状态机（显式状态 + 可测试恢复条件） ----------

def test_manifest_corrupt_alarm_not_cleared_same_poll(tmp_path, manifest):
    """P1-3 复现用例：损坏告警不得在列表为空的同一轮被清除。"""
    (tmp_path / "manifest.json").write_text("{not-json")
    client = FakeClient([])  # 列表为空
    sup, _ = _sup(client, manifest, tmp_path)
    sup.poll_once()
    assert (tmp_path / "alarm").exists()  # 修复前此文件会被同轮清除
    content = (tmp_path / "alarm").read_text()
    assert "manifest invalid" in content


def test_alarm_recovers_when_all_states_resolve(tmp_path, manifest):
    client = FakeClient(
        [_rec("sbx-x", tpl="other-tpl")], hydrate_map={}  # 非本轮模板，无告警
    )
    sup, _ = _sup(client, manifest, tmp_path)
    client.list_failures = 1
    sup.poll_once()
    assert (tmp_path / "alarm").exists()  # 控制面不可达 → 告警
    sup.poll_once()  # 恢复且无待确认 → 清除
    assert not (tmp_path / "alarm").exists()


def test_kill_failure_and_confirm_window_are_alarm_states(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})
    client.kill_mode = "error"
    sup, clocks = _sup(client, manifest, tmp_path)
    sup.poll_once()  # 观测锚定
    clocks.mono = DEADLINE_SECONDS
    assert sup.poll_once() == []  # kill 失败 → 不计已杀
    assert "kill failed" in (tmp_path / "alarm").read_text()

    client.kill_mode = "ok"
    clocks.mono = DEADLINE_SECONDS + CONFIRM_WINDOW_SECONDS + 1
    sup.poll_once()  # 重试成功但确认宽限已超 → 宽限告警
    assert "confirm window exceeded" in (tmp_path / "alarm").read_text()

    del client.records["sbx-a"]  # 目标消失 → 全部解除 → 清除
    sup.poll_once()
    assert not (tmp_path / "alarm").exists()


def test_hydrate_failure_is_alarm_state_and_skips_only_target(tmp_path, manifest):
    client = FakeClient(
        [_rec("sbx-bad"), _rec("sbx-ok")],
        hydrate_map={
            "sbx-bad": {},
            "sbx-ok": {"started_at": 1000.0, "run_marker": RUN},
        },
    )
    client.hydrate_failures = {"sbx-bad"}
    sup, clocks = _sup(client, manifest, tmp_path)
    sup.poll_once()  # 观测锚定（sbx-bad 的 hydrate 失败也在此轮记录）
    clocks.mono = DEADLINE_SECONDS
    killed = sup.poll_once()  # 单目标查询失败不拖累其他目标
    assert killed == ["sbx-ok"]
    assert "query error: sbx-bad" in (tmp_path / "alarm").read_text()


# ---------- P1-4：manifest 原子性 / schema / 旧轮拒绝 ----------

def test_manifest_atomic_replace_no_partial_reads(tmp_path):
    m = ManifestStore(tmp_path / "manifest.json")
    m.register_run(RUN, ALIAS, TPL, started_at=1000.0)
    stop = threading.Event()
    parse_errors = []

    def reader():
        while not stop.is_set():
            try:
                ManifestStore(tmp_path / "manifest.json").reload()
            except ManifestError as exc:
                parse_errors.append(str(exc))

    thread = threading.Thread(target=reader)
    thread.start()
    for i in range(50):
        m.register_sandbox(f"sbx-{i}")
    stop.set()
    thread.join()
    assert not parse_errors  # 并发读写全程只见过完整 JSON
    assert not list(tmp_path.glob("manifest.json.tmp-*"))  # 无残留临时文件


def test_manifest_rejects_invalid_schema(tmp_path, manifest):
    for bad in (
        {"run_id": 123},
        {"run_started_at": "not-a-number"},
        {"sandboxes": {"x": {"registered_at": "nope"}}},
        {"sandboxes": [1, 2]},
        "not-a-dict",
    ):
        (tmp_path / "manifest.json").write_text(json.dumps(bad))
        with pytest.raises(ManifestError):
            manifest.reload()


def test_stale_run_registration_rejected(tmp_path):
    m1 = ManifestStore(tmp_path / "manifest.json")
    m1.register_run("run-1", ALIAS, TPL, started_at=1000.0)
    m2 = ManifestStore(tmp_path / "manifest.json")  # 另一进程读到 run-1
    m2.register_run("run-2", ALIAS, TPL, started_at=2000.0)  # 新轮覆盖
    # m1 仍是旧轮上下文：延迟登记必须被拒（run_id 不匹配）
    with pytest.raises(ManifestError, match="stale run"):
        m1.register_sandbox("sbx-late")


def test_register_run_resets_and_cross_process_append(tmp_path):
    m1 = ManifestStore(tmp_path / "manifest.json")
    m1.register_run(RUN, ALIAS, TPL, started_at=1000.0)
    m1.register_sandbox("sbx-old")
    m1.register_run("run-2", ALIAS, TPL, started_at=2000.0)
    assert m1.registered_ids() == set()
    m2 = ManifestStore(tmp_path / "manifest.json")
    m2.register_sandbox("sbx-appended")
    m1.reload()
    assert m1.registered_ids() == {"sbx-appended"}
    assert m1.run_id == "run-2"


# ---------- 创建门禁（失败关闭） ----------

def test_gate_blocks_on_alarm(tmp_path, manifest):
    (tmp_path / "alarm").write_text("boom\n")
    allowed, reason = check_gate(tmp_path / "manifest.json", tmp_path / "alarm")
    assert allowed is False and "boom" in reason


def test_gate_blocks_on_invalid_manifest(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text("{bad")
    allowed, _detail = check_gate(tmp_path / "manifest.json", tmp_path / "alarm")
    assert allowed is False


def test_gate_fail_closed_on_missing_manifest(tmp_path):
    allowed, _ = check_gate(tmp_path / "absent.json", tmp_path / "alarm")
    assert allowed is False


def test_gate_allows_when_clean(tmp_path, manifest):
    allowed, detail = check_gate(tmp_path / "manifest.json", tmp_path / "alarm")
    assert allowed is True and detail == ""
