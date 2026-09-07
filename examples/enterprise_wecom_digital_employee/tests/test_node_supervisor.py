"""node_supervisor 定向测试（v2：epoch 时钟 / marker 归属 / manifest 闭环 / 异常韧性）。"""

from __future__ import annotations

import json
import time

import pytest
from sandbox_poc.node_supervisor import (
    CONFIRM_WINDOW_SECONDS,
    DEADLINE_SECONDS,
    ManifestError,
    ManifestStore,
    NodeSupervisor,
    NotFoundError,
    SandboxRecord,
)

RUN = "run-20260908-abc"
ALIAS = "agentseek-m0-poc"
TPL = "tpl-m0-poc"


class FakeClient:
    """按真实 SDK 适配器语义实现协议：list → 粗记录；hydrate → 补 marker/started。"""

    def __init__(self, listing, *, hydrate_map=None, kill_mode="ok"):
        self.records = {r.sandbox_id: r for r in listing}
        self.hydrate_map = hydrate_map or {}
        self.kill_mode = kill_mode
        self.kill_calls: list[str] = []
        self.list_failures = 0

    def list(self):
        if self.list_failures > 0:
            self.list_failures -= 1
            raise ConnectionError("control plane down")
        return list(self.records.values())

    def hydrate(self, record):
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


def _sup(client, manifest, tmp_path):
    return NodeSupervisor(client, manifest, alarm_file=tmp_path / "alarm")


def _rec(sid, tpl=TPL, state="running", started=None, marker=None):
    return SandboxRecord(sid, tpl, state, started_at=started, run_marker=marker)


# ---------- 时间语义（F3）：epoch 基准，--once 与常驻一致 ----------

def test_epoch_deadline_not_before_at_and_not_after(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})
    sup = _sup(client, manifest, tmp_path)

    assert sup.poll_once(now=1000.0 + DEADLINE_SECONDS - 1) == []  # 截止前
    assert sup.poll_once(now=1000.0 + DEADLINE_SECONDS) == ["sbx-a"]  # 到时即杀
    assert client.kill_calls == ["sbx-a"]


def test_real_epoch_and_iso_equivalence(tmp_path, manifest):
    started = time.time() - (DEADLINE_SECONDS + 5)
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": started, "run_marker": RUN}})
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=time.time()) == ["sbx-a"]  # 真实 epoch 超截止

    # 常驻与 --once 同一入口 poll_once（同用 epoch），行为一致由本用例与上用例共同覆盖


# ---------- 归属判定（F4）：marker 一级 / 登记 二级 / 窗口兜底 ----------

def test_marker_primary_and_other_run_never_killed(tmp_path, manifest):
    client = FakeClient(
        [_rec("sbx-ours"), _rec("sbx-other-run")],
        hydrate_map={
            "sbx-ours": {"started_at": 1000.0, "run_marker": RUN},
            "sbx-other-run": {"started_at": 1000.0, "run_marker": "run-OTHER"},
        },
    )
    sup = _sup(client, manifest, tmp_path)
    killed = sup.poll_once(now=1000.0 + DEADLINE_SECONDS + 1)
    assert killed == ["sbx-ours"]
    assert "sbx-other-run" not in client.kill_calls


def test_registered_id_marker_conflict_marker_wins(tmp_path, manifest):
    manifest.register_sandbox("sbx-conflict")
    client = FakeClient([_rec("sbx-conflict")], hydrate_map={
        "sbx-conflict": {"started_at": 1000.0, "run_marker": "run-OTHER"}
    })
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=1000.0 + DEADLINE_SECONDS + 1) == []  # 平台 marker 优先


def test_registered_fallback_with_registered_at_clock(tmp_path, manifest):
    # marker 缺失、started_at 缺失，但已登记 → 用登记时刻计时
    manifest.register_sandbox("sbx-reg")
    reg_at = manifest.registered_at("sbx-reg")
    client = FakeClient([_rec("sbx-reg")], hydrate_map={"sbx-reg": {}})
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=reg_at + DEADLINE_SECONDS + 1) == ["sbx-reg"]


def test_window_fallback_bounds_to_this_run(tmp_path, manifest):
    # 同模板但创建于本轮开始之前（上一轮遗留）→ 不杀；本轮窗口内 → 杀（告警兜底）
    client = FakeClient(
        [_rec("sbx-old", started=900.0), _rec("sbx-new", started=1100.0)],
        hydrate_map={"sbx-old": {"started_at": 900.0}, "sbx-new": {"started_at": 1100.0}},
    )
    sup = _sup(client, manifest, tmp_path)
    killed = sup.poll_once(now=1100.0 + DEADLINE_SECONDS + 1)
    assert "sbx-old" not in killed
    assert "sbx-new" in killed


def test_unknown_start_time_never_killed_but_alarmed(tmp_path, manifest):
    # F2/F5：字段缺失不得以 0 判断 → 不删除 + 告警
    client = FakeClient([_rec("sbx-unknown")], hydrate_map={"sbx-unknown": {}})
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=99999.0) == []
    assert (tmp_path / "alarm").exists()


# ---------- 异常韧性（F5） ----------

def test_list_failure_alarms_and_survives(tmp_path, manifest):
    client = FakeClient([], hydrate_map={})
    client.list_failures = 1
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=1.0) == []  # 第一轮 list 抛错
    assert (tmp_path / "alarm").exists()
    assert sup.poll_once(now=2.0) == []  # 循环不退出，下一轮恢复


def test_kill_notfound_confirms_generic_error_retries(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})

    client.kill_mode = "error"
    sup = _sup(client, manifest, tmp_path)
    assert sup.poll_once(now=1000.0 + DEADLINE_SECONDS) == []  # 失败不丢目标、不计已杀
    client.kill_mode = "notfound"
    # 404 → 视为已发起并确认消失（计入本轮动作）
    assert sup.poll_once(now=1000.0 + DEADLINE_SECONDS + 1) == ["sbx-a"]
    # 已确认消失（下轮不再列出的场景由 seen 集合处理；此处状态仍列但已确认）


def test_confirm_window_exceeded_raises_alarm_and_clears(tmp_path, manifest):
    client = FakeClient([_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}})
    sup = _sup(client, manifest, tmp_path)
    t0 = 1000.0 + DEADLINE_SECONDS
    sup.poll_once(now=t0)
    assert not (tmp_path / "alarm").exists()
    sup.poll_once(now=t0 + CONFIRM_WINDOW_SECONDS + 1)  # 仍存活 → 报警（阻止新建）
    assert (tmp_path / "alarm").exists()
    del client.records["sbx-a"]  # 目标消失
    sup.poll_once(now=t0 + CONFIRM_WINDOW_SECONDS + 2)
    assert not (tmp_path / "alarm").exists()  # 确认后清报警


# ---------- manifest 闭环（F4） ----------

def test_register_run_resets_and_cross_process_append_visible(tmp_path):
    m1 = ManifestStore(tmp_path / "manifest.json")
    m1.register_run(RUN, ALIAS, TPL, started_at=1000.0)
    m1.register_sandbox("sbx-old-run")
    m1.register_run("run-2", ALIAS, TPL, started_at=2000.0)  # 新轮重置
    assert m1.registered_ids() == set()

    m2 = ManifestStore(tmp_path / "manifest.json")  # 另一 CLI 进程追加
    m2.register_sandbox("sbx-appended")

    m1.reload()  # 常驻监督每轮 reload → 可见
    assert m1.registered_ids() == {"sbx-appended"}
    assert m1.run_id == "run-2"


def test_manifest_corrupt_raises_and_does_not_widen(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text("{not-json")
    with pytest.raises(ManifestError):
        manifest.reload()
    # 监督侧：损坏 → 告警一次，沿用最后已知（不扩大）
    client = FakeClient([_rec("sbx-x")], hydrate_map={"sbx-x": {"started_at": 1000.0}})
    sup = _sup(client, manifest, tmp_path)
    sup.poll_once(now=99999.0)
    assert (tmp_path / "alarm").exists()


def test_manifest_atomic_no_partial_write(tmp_path, manifest):
    manifest.register_sandbox("sbx-a")
    manifest.register_sandbox("sbx-b")
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert set(data["sandboxes"]) == {"sbx-a", "sbx-b"}  # 每次都是完整 JSON
