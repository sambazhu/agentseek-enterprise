"""portguard（R4 阶段化版）状态化测试。

用语义忠实的 iptables/ip6tables 模拟器（iptables_stub.py：-X 被引用拒绝、
-E 目标存在拒绝等）驱动真实脚本，逐步断言合同：
**失败后及再次执行期间，至少一个有效拒绝路径始终在位**（有效=跳转在
INPUT 且链内规则与期望逐行全等）——不满足于检查"是否出现某条命令"。
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PORTGUARD = str(HERE.parent / "sandbox_poc" / "portguard.sh")
STUB_SRC = (HERE / "iptables_stub.py").read_text()


def _canonical(fam: str) -> list[list[str]]:
    """期望规则（裸 token，与 stub 状态存储形态一致）。"""
    rules: list[list[str]] = []
    for port in (9999, 8082):
        if fam == "iptables":
            rules.append(f"-s 127.0.0.1/32 -p tcp -m tcp --dport {port} -j ACCEPT".split())
            rules.append(f"-s 192.10.50.172/32 -p tcp -m tcp --dport {port} -j ACCEPT".split())
        else:
            rules.append(f"-s ::1/128 -p tcp -m tcp --dport {port} -j ACCEPT".split())
        rules.append(f"-p tcp -m tcp --dport {port} -j REJECT --reject-with tcp-reset".split())
    return rules


def _canonical_seed(fam: str) -> dict[str, list]:
    return {
        "INPUT": [["-j", "CUBE_POC_GUARD"]],
        "CUBE_POC_GUARD": _canonical(fam),
    }


class _Env:
    """受控环境：stub 双家族 + 独立状态 + 真实 flock 锁文件。"""

    def __init__(self, seed_v4: dict | None, seed_v6: dict | None):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.root = root
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        for name in ("iptables", "ip6tables"):
            stub = self.bin_dir / name
            stub.write_text(STUB_SRC)
            stub.chmod(0o755)
        self.state_base = root / "state"
        self.log = root / "ops.log"
        self.lock = root / "guard.lock"
        for fam, seed in (("iptables", seed_v4), ("ip6tables", seed_v6)):
            chains: dict[str, list] = {"INPUT": [], "FORWARD": [], "OUTPUT": []}
            if seed:
                chains.update(seed)
            (root / f"state.{fam}.json").write_text(json.dumps({"chains": chains}))

    def _env(self, *, tag: str, fail: str, wait: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "CUBE_STUB_STATE": str(self.state_base),
            "CUBE_STUB_LOG": str(self.log),
            "CUBE_STUB_FAIL": fail,
            "CUBE_STUB_TAG": tag,
            "CUBE_POC_GUARD_LOCK": str(self.lock),
            "CUBE_POC_GUARD_LOCK_WAIT": wait,
        }

    def run(self, op: str = "apply", *, fail: str = "", tag: str = "run",
            wait: str = "10", timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", PORTGUARD, op], capture_output=True, text=True,
            timeout=timeout, env=self._env(tag=tag, fail=fail, wait=wait),
        )

    def popen(self, *, tag: str, wait: str = "30") -> subprocess.Popen:
        return subprocess.Popen(  # noqa: S603
            ["/bin/bash", PORTGUARD, "apply"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=self._env(tag=tag, fail="", wait=wait),
        )

    def state(self, fam: str) -> dict:
        return json.loads((self.root / f"state.{fam}.json").read_text())

    def write_state(self, fam: str, state: dict) -> None:
        (self.root / f"state.{fam}.json").write_text(json.dumps(state))

    def snaps(self, fam: str) -> list[dict]:
        out = []
        for ln in self.log.read_text().splitlines():
            parts = ln.split(" ", 3)
            if parts[0] == "SNAP" and parts[1] == fam:
                out.append(json.loads(parts[3]))
        return out

    def close(self) -> None:
        self._td.cleanup()


def _effective(state: dict, fam: str) -> bool:
    """有效保护=存在某链：其跳转在 INPUT 且规则与期望逐行全等。"""
    jumps = state["chains"].get("INPUT", [])
    return any(
        ["-j", name] in jumps and rules == _canonical(fam)
        for name, rules in state["chains"].items()
    )


def _assert_protection_never_lost(env: _Env, fam: str) -> None:
    snaps = env.snaps(fam)
    assert snaps, "无快照"
    for idx, st in enumerate(snaps):
        assert _effective(st, fam), f"{fam} 第 {idx} 个快照丢失有效保护: {st}"


def _assert_canonical_final(env: _Env, fam: str) -> None:
    st = env.state(fam)
    assert st["chains"]["INPUT"] == [["-j", "CUBE_POC_GUARD"]], st["chains"]["INPUT"]
    assert st["chains"]["CUBE_POC_GUARD"] == _canonical(fam), st["chains"]["CUBE_POC_GUARD"]
    assert "CUBE_POC_GUARD.new" not in st["chains"]


# ---------- 正常路径 ----------

def test_apply_from_canonical_is_stable_both_families():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        proc = env.run()
        assert proc.returncode == 0, proc.stderr
        _assert_canonical_final(env, "iptables")
        _assert_canonical_final(env, "ip6tables")
        _assert_protection_never_lost(env, "iptables")
        _assert_protection_never_lost(env, "ip6tables")
    finally:
        env.close()


def test_apply_from_empty_state_builds_both_families():
    env = _Env(None, None)
    try:
        proc = env.run()
        assert proc.returncode == 0, proc.stderr
        _assert_canonical_final(env, "iptables")
        _assert_canonical_final(env, "ip6tables")
    finally:
        env.close()


# ---------- 接管前失败：OLD 原样保留 ----------

@pytest.mark.parametrize("op", ["N", "A", "I"])
def test_early_failure_preserves_old(op: str):
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        proc = env.run(fail=f"iptables:{op}")
        assert proc.returncode != 0
        # v4 终态=种子原样（OLD 未动、无 NEW 残留）
        seed_chains = {"INPUT": [["-j", "CUBE_POC_GUARD"]],
                       "FORWARD": [], "OUTPUT": [],
                       "CUBE_POC_GUARD": _canonical("iptables")}
        assert env.state("iptables")["chains"] == seed_chains
        # 脚本在 v4 失败处中止，v6 未被触碰
        assert env.state("ip6tables")["chains"]["CUBE_POC_GUARD"] == _canonical("ip6tables")
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


# ---------- 接管后失败：保留完整 NEW（临时名继续保护） ----------

def test_late_rename_failure_preserves_new():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        proc = env.run(fail="iptables:E")
        assert proc.returncode != 0
        st = env.state("iptables")
        assert ["-j", "CUBE_POC_GUARD.new"] in st["chains"]["INPUT"]
        assert st["chains"]["CUBE_POC_GUARD.new"] == _canonical("iptables")
        assert _effective(st, "iptables")  # 有效保护仍在（NEW 名义）
        assert env.state("ip6tables")["chains"]["CUBE_POC_GUARD"] == _canonical("ip6tables")
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


def test_reapply_after_late_failure_normalizes():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        first = env.run(fail="iptables:E", tag="first")
        assert first.returncode != 0
        second = env.run(tag="second")
        assert second.returncode == 0, second.stderr
        _assert_canonical_final(env, "iptables")
        _assert_canonical_final(env, "ip6tables")
        # 两次执行的全程快照都保持有效保护
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


def test_external_reference_late_failure_and_recovery():
    """外部链引用 OLD → -X/-E 自然失败（无需注入）→ NEW 保留；解除后收敛。"""
    seed = _canonical_seed("iptables")
    seed["CUBE_POC_DUMMY"] = [["-j", "CUBE_POC_GUARD"]]
    env = _Env(seed, _canonical_seed("ip6tables"))
    try:
        failed = env.run(tag="failrun")
        assert failed.returncode != 0
        st = env.state("iptables")
        assert _effective(st, "iptables")
        assert ["-j", "CUBE_POC_GUARD.new"] in st["chains"]["INPUT"]
        _assert_protection_never_lost(env, "iptables")
        # 解除外部引用（模拟人工清理）后再 apply → 收敛为规范名
        st["chains"].pop("CUBE_POC_DUMMY")
        env.write_state("iptables", st)
        ok = env.run(tag="recover")
        assert ok.returncode == 0, ok.stderr
        _assert_canonical_final(env, "iptables")
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


def test_duplicate_old_jumps_converge():
    seed = _canonical_seed("iptables")
    seed["INPUT"] = [["-j", "CUBE_POC_GUARD"], ["-j", "CUBE_POC_GUARD"]]
    env = _Env(seed, _canonical_seed("ip6tables"))
    try:
        proc = env.run()
        assert proc.returncode == 0, proc.stderr
        _assert_canonical_final(env, "iptables")  # 单跳转+完整规则
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


def test_v6_late_failure_leaves_v4_canonical():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        proc = env.run(fail="ip6tables:E")
        assert proc.returncode != 0
        _assert_canonical_final(env, "iptables")  # v4 先完成且规范
        st = env.state("ip6tables")
        assert _effective(st, "ip6tables")  # v6 NEW 名义保护在位
        _assert_protection_never_lost(env, "ip6tables")
    finally:
        env.close()


def test_polluted_jumped_new_refused_and_preserved():
    """NEW 挂了跳转但规则不完整（外部污染态）：拒绝执行且绝不自动拆除。"""
    seed = _canonical_seed("iptables")
    seed["CUBE_POC_GUARD.new"] = _canonical("iptables")[:3]  # 缺 REJECT
    seed["INPUT"] = [["-j", "CUBE_POC_GUARD.new"], ["-j", "CUBE_POC_GUARD"]]
    env = _Env(seed, _canonical_seed("ip6tables"))
    try:
        proc = env.run()
        assert proc.returncode != 0  # -N 失败退出（NEW 已存在）
        # 终态与种子完全一致：可能仍在提供部分拒绝的规则未被自动清理
        assert env.state("iptables")["chains"] == {
            **{"INPUT": seed["INPUT"], "FORWARD": [], "OUTPUT": []},
            "CUBE_POC_GUARD": _canonical("iptables"),
            "CUBE_POC_GUARD.new": seed["CUBE_POC_GUARD.new"],
        }
    finally:
        env.close()


# ---------- 互斥与显式 remove ----------

def test_parallel_applies_serialize():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        procs = [env.popen(tag=t) for t in ("a", "b")]
        for p in procs:
            out, _err = p.communicate(timeout=60)
            assert p.returncode == 0, out
        # flock 保证同一时刻仅一个实例：两个运行的快照不交错
        tags = [ln.split(" ", 3)[2] for ln in env.log.read_text().splitlines()
                if ln.startswith("SNAP")]
        assert set(tags) == {"a", "b"}
        pos_a = [i for i, t in enumerate(tags) if t == "a"]
        pos_b = [i for i, t in enumerate(tags) if t == "b"]
        assert max(pos_a) < min(pos_b) or max(pos_b) < min(pos_a)
        _assert_canonical_final(env, "iptables")
        _assert_protection_never_lost(env, "iptables")
    finally:
        env.close()


def test_lock_contention_returns_busy():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        with open(env.lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            proc = env.run(wait="1")
        assert proc.returncode == 75
        assert not env.log.exists()  # 未获得锁 → 未执行任何 iptables 操作
        assert env.state("iptables")["chains"]["CUBE_POC_GUARD"] == _canonical("iptables")
    finally:
        env.close()


def test_explicit_remove_tears_down_both_families():
    env = _Env(_canonical_seed("iptables"), _canonical_seed("ip6tables"))
    try:
        proc = env.run("remove")
        assert proc.returncode == 0, proc.stderr
        for fam in ("iptables", "ip6tables"):
            st = env.state(fam)
            assert st["chains"]["INPUT"] == []
            assert "CUBE_POC_GUARD" not in st["chains"]
            assert "CUBE_POC_GUARD.new" not in st["chains"]
    finally:
        env.close()
