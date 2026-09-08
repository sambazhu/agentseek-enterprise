"""preflight（R2 硬化版）定向测试：专用链结构校验、负向绕过、活动值解析。"""

from __future__ import annotations

from pathlib import Path

from sandbox_poc.preflight import (
    check_container_limits,
    check_egress_alive,
    check_firewall_structure,
    check_master_refs,
    check_port_convergence,
)

GOOD_DYN = "common:\n  meta_server_endpoint: \"\"\n  cubemaster_http_addr: \"127.0.0.1:18089\"\n"
GOOD_LCM = (
    "services:\n  clm:\n    mem_limit: 256m\n"
    "      CUBE_LCM_CUBEMASTER_URL:        \"http://127.0.0.1:18089\"\n"
    "      CUBE_LCM_LISTEN_ADDR:           \"127.0.0.1:8083\"\n"
)
GOOD_SS = "\n".join([
    "State Recv-Q Send-Queue Local Address:Port Peer Address:Port",
    "LISTEN 0 511 127.0.0.1:80 0.0.0.0:* users:((\"nginx\"))",
    "LISTEN 0 511 127.0.0.1:443 0.0.0.0:*",
    "LISTEN 0 511 127.0.0.1:9090 0.0.0.0:*",
    "LISTEN 0 511 127.0.0.1:9998 0.0.0.0:*",
    "LISTEN 0 511 127.0.0.1:9966 0.0.0.0:*",
    "LISTEN 0 511 127.0.0.1:8083 0.0.0.0:*",
    "LISTEN 0 511 *:9999 0.0.0.0:*",
    "LISTEN 0 511 192.10.50.172:8082 0.0.0.0:*",
])
HDR = "Chain INPUT (policy ACCEPT)\nnum target prot opt source destination"
GOOD_GUARD = HDR.replace("INPUT", "CUBE_POC_GUARD") + "\n" + "\n".join([
    "1 ACCEPT tcp -- 127.0.0.1 0.0.0.0/0 tcp dpt:9999",
    "2 ACCEPT tcp -- 192.10.50.172 0.0.0.0/0 tcp dpt:9999",
    "3 REJECT tcp -- 0.0.0.0/0 0.0.0.0/0 reject-with tcp-reset tcp dpt:9999",
    "4 ACCEPT tcp -- 127.0.0.1 0.0.0.0/0 tcp dpt:8082",
    "5 ACCEPT tcp -- 192.10.50.172 0.0.0.0/0 tcp dpt:8082",
    "6 REJECT tcp -- 0.0.0.0/0 0.0.0.0/0 reject-with tcp-reset tcp dpt:8082",
])
GOOD_INPUT = HDR + "\n1 CUBE_POC_GUARD all -- 0.0.0.0/0 0.0.0.0/0\n2 ACCEPT all -- 0.0.0.0/0 0.0.0.0/0"


def _read(files: dict[str, str]):
    def reader(p: Path) -> str:
        return files[str(p)]
    return reader


# ---------- master_refs：活动值级校验 ----------

def test_master_refs_good():
    files = {
        "/t/Cubelet/dynamicconf/conf.yaml": GOOD_DYN,
        "/t/cube-lifecycle-manager/docker-compose.yaml": GOOD_LCM,
    }
    ok, detail = check_master_refs(_read(files), Path("/t"))
    assert ok, detail


def test_master_refs_comment_cannot_mask_active_value():
    """R2 复现缺陷：注释里的正确地址不能掩盖活动值为空/错误。"""
    masked_dyn = (
        "# cubemaster_http_addr: \"127.0.0.1:18089\"\n"
        "  cubemaster_http_addr: \"\"\n"
    )
    masked_lcm = (
        "# CUBE_LCM_CUBEMASTER_URL: \"http://127.0.0.1:18089\"\n"
        "      CUBE_LCM_CUBEMASTER_URL: \"http://192.10.50.172:8089\"\n"
        "      CUBE_LCM_LISTEN_ADDR: \"0.0.0.0:8083\"\n"
    )
    files = {
        "/t/Cubelet/dynamicconf/conf.yaml": masked_dyn,
        "/t/cube-lifecycle-manager/docker-compose.yaml": masked_lcm,
    }
    ok, detail = check_master_refs(_read(files), Path("/t"))
    assert not ok
    assert "dynamicconf=''" in detail  # 空活动值被抓


def test_master_refs_ambiguous_multiple_active_keys():
    files = {
        "/t/Cubelet/dynamicconf/conf.yaml": GOOD_DYN,
        "/t/cube-lifecycle-manager/docker-compose.yaml": (
            "CUBE_LCM_CUBEMASTER_URL: \"http://127.0.0.1:18089\"\n"
            "CUBE_LCM_CUBEMASTER_URL: \"http://127.0.0.1:18089\"\n"
            "CUBE_LCM_LISTEN_ADDR: \"127.0.0.1:8083\"\n"
        ),
    }
    ok, detail = check_master_refs(_read(files), Path("/t"))
    assert not ok and "LCM URL 活动键 2 处" in detail


# ---------- 限额 ----------

def test_container_limits_exact_drift_and_cmdfail():
    from sandbox_poc.preflight import EXPECTED_CONTAINER_MEMORY

    def ok_inspect(name: str) -> tuple[int, str]:
        return 0, str(EXPECTED_CONTAINER_MEMORY[name] * 1024 * 1024) + "\n"

    assert check_container_limits(ok_inspect)[0]

    def drift(name: str) -> tuple[int, str]:
        return (0, "0\n") if name == "cube-lifecycle-manager" else ok_inspect(name)

    ok, detail = check_container_limits(drift)
    assert not ok and "cube-lifecycle-manager=0MiB" in detail

    def fail(name: str) -> tuple[int, str]:
        return 1, "Error response from daemon\n"

    ok, detail = check_container_limits(fail)
    assert not ok and "cmd-fail" in detail  # 命令失败必须拒绝


# ---------- 防火墙结构（R2 核心） ----------

def test_firewall_structure_good():
    ok, detail = check_firewall_structure(GOOD_INPUT, GOOD_GUARD)
    assert ok, detail


def test_firewall_structure_preceding_accept_all_rejected():
    """R2 复现缺陷：INPUT 前置全放行把守卫挤到非首位 → 必须拒绝。"""
    bad_input = HDR + "\n1 ACCEPT all -- 0.0.0.0/0 0.0.0.0/0\n2 CUBE_POC_GUARD all -- 0.0.0.0/0 0.0.0.0/0"
    ok, detail = check_firewall_structure(bad_input, GOOD_GUARD)
    assert not ok and "首条非 CUBE_POC_GUARD" in detail
    ok, _ = check_firewall_structure(HDR + "\n", GOOD_GUARD)
    assert not ok  # 无跳转


def test_firewall_structure_wrong_source_and_port_token():
    """错误来源放行 / 端口 token 前缀相似（dpt:80820）都必须拒绝。"""
    wrong_src = GOOD_GUARD.replace(
        "4 ACCEPT tcp -- 127.0.0.1 0.0.0.0/0 tcp dpt:8082",
        "4 ACCEPT tcp -- 0.0.0.0/0 0.0.0.0/0 tcp dpt:8082",
    )
    ok, _ = check_firewall_structure(GOOD_INPUT, wrong_src)
    assert not ok
    wrong_port = GOOD_GUARD.replace("dpt:8082", "dpt:80820").replace("dpt:80820 ", "dpt:80820 ")
    ok, _ = check_firewall_structure(GOOD_INPUT, wrong_port)
    assert not ok
    missing = "\n".join(GOOD_GUARD.splitlines()[:-1])  # 少末条 REJECT
    ok, detail = check_firewall_structure(GOOD_INPUT, missing)
    assert not ok and "规则数" in detail


def test_port_convergence_command_failure_refuses():
    """读取失败（rc≠0）必须拒绝，不得解析输出放行。"""
    ok, detail = check_port_convergence((1, GOOD_SS), (0, GOOD_INPUT), (0, GOOD_GUARD))
    assert not ok and "ss 命令失败" in detail
    ok, detail = check_port_convergence((0, GOOD_SS), (0, GOOD_INPUT), (2, "iptables: No chain"))
    assert not ok and "iptables-guard 命令失败" in detail


def test_port_convergence_nonloopback_bind():
    bad_ss = GOOD_SS.replace("127.0.0.1:80 ", "0.0.0.0:80 ")
    ok, detail = check_port_convergence((0, bad_ss), (0, GOOD_INPUT), (0, GOOD_GUARD))
    assert not ok and "80=非loopback" in detail


# ---------- egress ----------

def test_egress_alive_detects_down():
    assert check_egress_alive(lambda h, p: True)[0]
    ok, detail = check_egress_alive(lambda h, p: (_ for _ in ()).throw(OSError("refused")))
    assert not ok and "不可达" not in detail  # 异常路径也要失败关闭
    assert not check_egress_alive(lambda h, p: False)[0]
