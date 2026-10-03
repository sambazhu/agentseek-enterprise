"""preflight（R2/R3/R4 硬化版）定向测试：专用链结构校验、负向绕过、活动值解析。

portguard.sh 的状态化测试（有效保护不变量）见 test_portguard.py。"""

from __future__ import annotations

from pathlib import Path

from sandbox_poc.preflight import (
    check_container_limits,
    check_egress_alive,
    check_firewall6,
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
GOOD_GUARD_S = "\n".join([
    "-A CUBE_POC_GUARD -s 127.0.0.1/32 -p tcp -m tcp --dport 9999 -j ACCEPT",
    "-A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport 9999 -j ACCEPT",
    "-A CUBE_POC_GUARD -p tcp -m tcp --dport 9999 -j REJECT --reject-with tcp-reset",
    "-A CUBE_POC_GUARD -s 127.0.0.1/32 -p tcp -m tcp --dport 8082 -j ACCEPT",
    "-A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport 8082 -j ACCEPT",
    "-A CUBE_POC_GUARD -p tcp -m tcp --dport 8082 -j REJECT --reject-with tcp-reset",
])
GOOD_INPUT_S = "-P INPUT ACCEPT\n-A INPUT -j CUBE_POC_GUARD\n-A INPUT -j ufw-before-input\n"
GOOD_GUARD6_S = "\n".join([
    "-A CUBE_POC_GUARD -s ::1/128 -p tcp -m tcp --dport 9999 -j ACCEPT",
    "-A CUBE_POC_GUARD -p tcp -m tcp --dport 9999 -j REJECT --reject-with tcp-reset",
    "-A CUBE_POC_GUARD -s ::1/128 -p tcp -m tcp --dport 8082 -j ACCEPT",
    "-A CUBE_POC_GUARD -p tcp -m tcp --dport 8082 -j REJECT --reject-with tcp-reset",
])
GOOD_INPUT6_S = "-P INPUT ACCEPT\n-A INPUT -j CUBE_POC_GUARD\n"


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


# ---------- 防火墙结构（R3：iptables -S 完整形态全等） ----------

def test_firewall_structure_good():
    ok, detail = check_firewall_structure(GOOD_INPUT_S, GOOD_GUARD_S)
    assert ok, detail


def test_firewall_conditional_jump_bypass_rejected():
    """R3 复现缺陷：INPUT 首条为**条件跳转**（仅 127.0.0.1 源进入守卫）。

    外部流量不进守卫链——即便链内规则完全正确也必须拒绝。
    """
    bad = "-P INPUT ACCEPT\n-A INPUT -s 127.0.0.1/32 -j CUBE_POC_GUARD\n-A INPUT -j ACCEPT\n"
    ok, detail = check_firewall_structure(bad, GOOD_GUARD_S)
    assert not ok and "无条件跳转" in detail
    # 跳转带接口/协议条件同样拒绝
    bad2 = "-P INPUT ACCEPT\n-A INPUT -i ens192 -j CUBE_POC_GUARD\n"
    ok, _ = check_firewall_structure(bad2, GOOD_GUARD_S)
    assert not ok


def test_firewall_extra_conditions_on_rules_rejected():
    """REJECT 带 -s 限缩（拒绝不到外部）/ ACCEPT 带 -d 限缩 → 形态不等拒绝。"""
    reject_limited = GOOD_GUARD_S.replace(
        "-A CUBE_POC_GUARD -p tcp -m tcp --dport 9999 -j REJECT",
        "-A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport 9999 -j REJECT",
    )
    ok, detail = check_firewall_structure(GOOD_INPUT_S, reject_limited)
    assert not ok and "形态不等" in detail
    accept_dst = GOOD_GUARD_S.replace(
        "-A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport 9999 -j ACCEPT",
        "-A CUBE_POC_GUARD -s 192.10.50.172/32 -d 127.0.0.1/32 -p tcp -m tcp --dport 9999 -j ACCEPT",
    )
    ok, _ = check_firewall_structure(GOOD_INPUT_S, accept_dst)
    assert not ok


def test_firewall_preceding_rules_and_missing_jump():
    """前置全放行（跳转非首条）/ 无跳转 → 拒绝。"""
    bad = "-P INPUT ACCEPT\n-A INPUT -j ACCEPT\n-A INPUT -j CUBE_POC_GUARD\n"
    ok, detail = check_firewall_structure(bad, GOOD_GUARD_S)
    assert not ok and "无条件跳转" in detail
    ok, _ = check_firewall_structure("-P INPUT ACCEPT\n-A INPUT -j ACCEPT\n", GOOD_GUARD_S)
    assert not ok


def test_port_convergence_command_failure_refuses():
    """读取失败（rc≠0）必须拒绝，不得解析输出放行。"""
    ok, detail = check_port_convergence((1, GOOD_SS), (0, GOOD_INPUT_S), (0, GOOD_GUARD_S))
    assert not ok and "ss 命令失败" in detail
    ok, detail = check_port_convergence((0, GOOD_SS), (0, GOOD_INPUT_S), (2, "iptables: No chain"))
    assert not ok and "iptables-guard 命令失败" in detail


def test_port_convergence_nonloopback_bind():
    bad_ss = GOOD_SS.replace("127.0.0.1:80 ", "0.0.0.0:80 ")
    ok, detail = check_port_convergence((0, bad_ss), (0, GOOD_INPUT_S), (0, GOOD_GUARD_S))
    assert not ok and "80=非loopback" in detail


# ---------- 防火墙 v6（R4：[::]:9999 双栈监听 → 等效 v6 保护） ----------

def test_firewall6_good():
    ok, detail = check_firewall6((0, GOOD_INPUT6_S), (0, GOOD_GUARD6_S))
    assert ok, detail


def test_firewall6_conditional_jump_and_limited_reject_rejected():
    """v6 条件跳转（仅 ::1 进守卫）= 同链路客户端绕过 → 拒绝。"""
    bad = "-P INPUT ACCEPT\n-A INPUT -s ::1/128 -j CUBE_POC_GUARD\n"
    ok, detail = check_firewall6((0, bad), (0, GOOD_GUARD6_S))
    assert not ok and "无条件跳转" in detail
    # REJECT 带 -s 限缩（link-local 客户端不被拒）→ 形态不等拒绝
    limited = GOOD_GUARD6_S.replace(
        "-A CUBE_POC_GUARD -p tcp -m tcp --dport 9999 -j REJECT",
        "-A CUBE_POC_GUARD -s fe80::/10 -p tcp -m tcp --dport 9999 -j REJECT",
    )
    ok, detail = check_firewall6((0, GOOD_INPUT6_S), (0, limited))
    assert not ok and "形态不等" in detail


def test_firewall6_command_failure_and_missing_chain_refused():
    ok, detail = check_firewall6((1, "iptables: command fail"), (0, GOOD_GUARD6_S))
    assert not ok and "命令失败" in detail
    ok, detail = check_firewall6((0, GOOD_INPUT6_S), (0, ""))
    assert not ok  # 链缺失/无规则 → 拒绝


# ---------- egress ----------

def test_egress_alive_detects_down():
    assert check_egress_alive(lambda h, p: True)[0]
    ok, detail = check_egress_alive(lambda h, p: (_ for _ in ()).throw(OSError("refused")))
    assert not ok and "不可达" not in detail  # 异常路径也要失败关闭
    assert not check_egress_alive(lambda h, p: False)[0]
