"""node_supervisor 定向测试（v3：P1×4/P2 修复的故障路径全覆盖）。"""

from __future__ import annotations

import json
import threading

import pytest
from sandbox_poc.node_supervisor import (
    CONFIRM_WINDOW_SECONDS,
    DEADLINE_SECONDS,
    RUN_MARKER_KEY,
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


# ---------- 安装轮实测缺陷：平台 startedAt 纳秒精度 × Python 3.10 ----------

def test_parse_epoch_format_matrix():
    """显式定义的平台时间接受格式（Codex 评审 R1-Q2 要求）。

    小数 0/1/2/3/4/5/6/9 位（右补零/超 6 位截断）、Z 与显式时区（含
    紧凑 ±HHMM）、数值原样；朴素时间（无时区）一律拒绝，绝不按宿主
    本地时区解释；非法/越界/None → None。期望值用 timezone.utc 构造，
    本测试在 py3.10 与 py3.13 行为一致。
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from sandbox_poc.node_supervisor import _parse_epoch

    base = _dt(2026, 9, 7, 15, 22, 14, tzinfo=_tz.utc).timestamp()
    # 小数位数矩阵：digits → 期望微秒（>6 位截断到微秒）
    for digits, micro in [
        ("1", 100_000), ("25", 250_000), ("123", 123_000), ("1234", 123_400),
        ("12345", 123_450), ("123456", 123_456), ("123456789", 123_456),
    ]:
        got = _parse_epoch(f"2026-09-07T15:22:14.{digits}Z")
        assert got == pytest.approx(base + micro / 1e6), digits
    # 无小数
    assert _parse_epoch("2026-09-07T15:22:14Z") == pytest.approx(base)
    assert _parse_epoch("2026-09-07t15:22:14z") == pytest.approx(base)
    assert _parse_epoch("2026-09-07 15:22:14Z") == pytest.approx(base)
    # 显式时区
    assert _parse_epoch("2026-09-07T15:22:14+00:00") == pytest.approx(base)
    assert _parse_epoch("2026-09-07T23:22:14+08:00") == pytest.approx(base)
    assert _parse_epoch("2026-09-07T23:22:14+0800") == pytest.approx(base)
    assert _parse_epoch("2026-09-07T07:22:14-08:00") == pytest.approx(base)
    # 数值原样 / 非有限 → None
    assert _parse_epoch(1788794534.5) == 1788794534.5
    assert _parse_epoch(float("nan")) is None
    assert _parse_epoch(float("inf")) is None
    # 拒绝：朴素（无时区）、非法、越界日期、非规范数字宽度
    for bad in [
        "2026-09-07T15:22:14", "2026-09-07 15:22:14", "not-a-time", "",
        "2026-13-01T00:00:00Z", "2026-09-07T25:00:00Z", "2026-9-7T15:22:14Z",
        "2026-09-07T15:22:14.1234567890Z", None, {"x": 1},
    ]:
        assert _parse_epoch(bad) is None, bad


def test_hydrate_with_nanosecond_startedat_anchors_deadline(tmp_path, manifest):
    """端到端：纳秒精度 startedAt 经真实解析路径锚定 deadline 并到时终止。"""
    from sandbox_poc.node_supervisor import HttpCubeClient

    info = {"startedAt": "2026-09-07T15:22:14.258327917Z",
            "metadata": {RUN_MARKER_KEY: RUN}}
    real_client = HttpCubeClient("http://127.0.0.1:1", timeout=0.1)
    real_client._request = lambda method, path: info  # type: ignore[assignment]
    real_client.list = lambda: [SandboxRecord("sbx-nano", TPL, "running")]  # type: ignore[assignment]
    out = real_client.hydrate(SandboxRecord("sbx-nano", TPL, "running"))
    assert out.started_at is not None and out.run_marker == RUN
    # 首观时钟对齐 startedAt → deadline_mono = +120；到时必须终止
    sup, clocks = _sup(real_client, manifest, tmp_path)
    clocks.epoch = out.started_at
    sup.poll_once()  # 观测锚定
    clocks.mono = DEADLINE_SECONDS
    assert sup.poll_once() == ["sbx-nano"]


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

def _write_heartbeat(tmp_path, run_id=RUN, ts=1000.0):
    import json as _json

    beat = tmp_path / "manifest.json.heartbeat"
    beat.write_text(_json.dumps({"run_id": run_id, "ts": ts, "pid": 1}))
    return beat


def test_gate_blocks_on_alarm(tmp_path, manifest):
    (tmp_path / "alarm").write_text("boom\n")
    _write_heartbeat(tmp_path)
    allowed, _detail = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1001.0
    )
    assert allowed is False


def test_gate_blocks_on_invalid_manifest(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text("{bad")
    _write_heartbeat(tmp_path)
    allowed, _detail = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1001.0
    )
    assert allowed is False


def test_gate_fail_closed_on_missing_manifest(tmp_path):
    allowed, _ = check_gate(tmp_path / "absent.json", tmp_path / "alarm", now=1001.0)
    assert allowed is False


def test_gate_rejects_when_supervisor_never_started(tmp_path, manifest):
    """登记 run 但未启动监督（无心跳）→ 拒绝。"""
    allowed, reason = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1001.0
    )
    assert allowed is False and "no heartbeat" in reason


def test_gate_rejects_stale_heartbeat_after_supervisor_exit(tmp_path, manifest):
    """监督退出后心跳过期 → 拒绝。"""
    _write_heartbeat(tmp_path, ts=1000.0)
    allowed, reason = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1000.0 + 3600.0
    )
    assert allowed is False and "stale" in reason


def test_gate_rejects_heartbeat_from_previous_run(tmp_path, manifest):
    """旧轮心跳（run_id 不匹配）→ 拒绝。"""
    _write_heartbeat(tmp_path, run_id="run-OLD", ts=1000.0)
    allowed, reason = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1001.0
    )
    assert allowed is False and "mismatch" in reason


def test_gate_allows_when_clean(tmp_path, manifest):
    _write_heartbeat(tmp_path, ts=1000.0)
    allowed, detail = check_gate(
        tmp_path / "manifest.json", tmp_path / "alarm", now=1001.0
    )
    assert allowed is True and detail == ""


def test_supervisor_poll_writes_fresh_heartbeat(tmp_path, manifest):
    client = FakeClient([_rec("sbx-x", tpl="other-tpl")])
    sup, _ = _sup(client, manifest, tmp_path)
    sup.poll_once()
    beat = tmp_path / "manifest.json.heartbeat"
    assert beat.exists()
    assert RUN in beat.read_text()


# ---------- 二：list 失败保留目标状态（deadline/确认计时不重置） ----------

def test_list_failure_preserves_targets_and_timers(tmp_path, manifest):
    """首杀→list 故障→恢复：deadline_mono 与 first_kill_mono 均不重置。"""
    client = FakeClient(
        [_rec("sbx-a")], hydrate_map={"sbx-a": {"started_at": 1000.0, "run_marker": RUN}}
    )
    sup, clocks = _sup(client, manifest, tmp_path)
    sup.poll_once()  # 观测锚定（mono=0 → deadline_mono=120）
    clocks.mono = DEADLINE_SECONDS
    sup.poll_once()  # 首杀（first_kill_mono=120）
    deadline_before = sup._targets["sbx-a"].deadline_mono
    first_kill_before = sup._targets["sbx-a"].first_kill_mono

    client.list_failures = 1
    clocks.mono = DEADLINE_SECONDS + 10
    sup.poll_once()  # 故障轮：不得清空目标状态
    assert "sbx-a" in sup._targets
    assert sup._targets["sbx-a"].deadline_mono == deadline_before
    assert sup._targets["sbx-a"].first_kill_mono == first_kill_before

    clocks.mono = DEADLINE_SECONDS + CONFIRM_WINDOW_SECONDS + 2
    sup.poll_once()  # 恢复：确认宽限按原始 first_kill 判定
    assert "confirm window exceeded" in (tmp_path / "alarm").read_text()
    assert client.kill_calls.count("sbx-a") >= 2
