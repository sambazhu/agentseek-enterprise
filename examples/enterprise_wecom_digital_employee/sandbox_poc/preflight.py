"""PoC 创建前预检（.172 侧，失败关闭）——R2/R3/R4 评审硬化版。

每次新建沙箱前与 gate-check 一并调用（exit 0=放行 / 3=阻断）。检查项：
1. master 活动引用：dynamicconf `cubemaster_http_addr` 与 LCM compose
   `CUBE_LCM_CUBEMASTER_URL` 的**活动值**（跳过注释行，多处活动出现视为
   歧义拒绝）必须精确等于 127.0.0.1:18089——注释里的正确地址不能掩盖
   活动值为空/错误（R2 复现缺陷）；
2. 容器限额：8 容器 docker inspect Memory 必须等于冻结值；
3. 端口收敛+防火墙结构：指定端口必须绑 127.0.0.1；9999/8082 必须由
   **专用链 CUBE_POC_GUARD** 保护——按 `iptables -S` **完整形态全等**
   校验（R3）：INPUT 首条必须为**无条件**跳转（条件跳转=绕过）、链内
   每条规则 token 级全等（REJECT 带 -s 限缩/ACCEPT 带 -d 限缩均拒绝）；
   **命令 returncode≠0 一律拒绝**；
4. 防火墙 v6（R4）：cubelet 于 [::]:9999 双栈监听，实测同链路客户端可经
   link-local 绕过 v4 守卫 → `ip6tables -S` 同构完整形态校验
   （9999/8082 各两条：::1 ACCEPT + 无条件 REJECT）；
5. egress 存活：127.0.0.1:9091 可达；
6. 监督心跳门禁（复用 check_gate）。

读取器/执行器全部可注入；纯标准库。R1 版教训：文本出现≠规则有效。
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

try:  # 包内导入（测试经 sandbox_poc 包）；失败则按同目录独立脚本部署回退
    from .node_supervisor import check_gate
except ImportError:
    from node_supervisor import check_gate

EXPECTED_MASTER_ADDR = "127.0.0.1:18089"
GUARD_CHAIN = "CUBE_POC_GUARD"


def _rule(port: int, source: str | None, target: str) -> list[str]:
    """构造 iptables -S 完整规则形态（与 portguard.sh 输出严格一致）。"""
    tokens = ["-A", GUARD_CHAIN]
    if source is not None:
        tokens += ["-s", source]
    tokens += ["-p", "tcp", "-m", "tcp", "--dport", str(port), "-j", target]
    if target == "REJECT":
        tokens += ["--reject-with", "tcp-reset"]
    return tokens


# 期望规则（有序，**完整 token 全等**——多一个条件/少一个条件都拒绝）
GUARD_RULES: list[list[str]] = [
    _rule(9999, "127.0.0.1/32", "ACCEPT"),
    _rule(9999, "192.10.50.172/32", "ACCEPT"),
    _rule(9999, None, "REJECT"),
    _rule(8082, "127.0.0.1/32", "ACCEPT"),
    _rule(8082, "192.10.50.172/32", "ACCEPT"),
    _rule(8082, None, "REJECT"),
]
# v6 期望规则（R4）：节点无全局 v6、平台自连走 v4，仅放行 ::1
GUARD6_RULES: list[list[str]] = [
    _rule(9999, "::1/128", "ACCEPT"),
    _rule(9999, None, "REJECT"),
    _rule(8082, "::1/128", "ACCEPT"),
    _rule(8082, None, "REJECT"),
]
# INPUT 首条必须为**无条件**跳转（无 -s/-d/-i/-o/-p 等任何匹配项）
INPUT_JUMP: list[str] = ["-A", "INPUT", "-j", GUARD_CHAIN]
EXPECTED_CONTAINER_MEMORY: dict[str, int] = {
    "cube-sandbox-mysql": 512,
    "cube-sandbox-redis": 128,
    "cube-sandbox-minio": 768,
    "cube-proxy-coredns": 64,
    "cube-webui": 64,
    "cube-proxy": 512,
    "cube-lifecycle-manager": 256,
    "cube-egress": 256,
}
LOOPBACK_BIND_PORTS = (80, 443, 9090, 9998, 9966, 8083)


def _run(cmd: list[str]) -> tuple[int, str]:
    # 固定白名单命令（本模块自有 ss/iptables/docker inspect），无外部输入拼接
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)  # noqa: S603
    return proc.returncode, proc.stdout + proc.stderr


def _active_value(text: str, key: str) -> list[str]:
    """取配置中该键的活动值列表（跳过注释行；值去引号）。"""
    values = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(rf"^\s*{re.escape(key)}\s*:", stripped):
            raw = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            values.append(raw)
    return values


def check_master_refs(read_file: Callable[[Path], str], toolbox: Path) -> tuple[bool, str]:
    """master 活动引用必须精确等于 127.0.0.1:18089（活动值，非全文子串）。"""
    problems: list[str] = []
    dyn = read_file(toolbox / "Cubelet/dynamicconf/conf.yaml")
    dyn_values = _active_value(dyn, "cubemaster_http_addr")
    if len(dyn_values) != 1:
        problems.append(f"dynamicconf 活动键 {len(dyn_values)} 处（歧义/缺失）")
    elif dyn_values[0] != EXPECTED_MASTER_ADDR:
        problems.append(f"dynamicconf={dyn_values[0]!r}")

    lcm = read_file(toolbox / "cube-lifecycle-manager/docker-compose.yaml")
    lcm_values = _active_value(lcm, "CUBE_LCM_CUBEMASTER_URL")
    if len(lcm_values) != 1:
        problems.append(f"LCM URL 活动键 {len(lcm_values)} 处")
    elif lcm_values[0] != f"http://{EXPECTED_MASTER_ADDR}":
        problems.append(f"LCM URL={lcm_values[0]!r}")
    listen_values = _active_value(lcm, "CUBE_LCM_LISTEN_ADDR")
    if len(listen_values) != 1 or not listen_values[0].startswith("127.0.0.1:"):
        problems.append(f"LCM LISTEN={listen_values!r}")

    if problems:
        return False, "; ".join(problems)
    return True, f"master 活动引用={EXPECTED_MASTER_ADDR}（值级校验）"


def check_container_limits(docker_inspect: Callable[[str], tuple[int, str]]) -> tuple[bool, str]:
    """8 容器限额=冻结值；inspect 执行失败/不可读一律拒绝。"""
    bad = []
    for name, mib in EXPECTED_CONTAINER_MEMORY.items():
        rc, out = docker_inspect(name)
        if rc != 0:
            bad.append(f"{name}=cmd-fail(rc={rc})")
            continue
        try:
            memory = int(out.strip().splitlines()[0])
        except (ValueError, IndexError):
            bad.append(f"{name}=unreadable")
            continue
        if memory != mib * 1024 * 1024:
            bad.append(f"{name}={memory // (1024 * 1024)}MiB")
    if bad:
        return False, "限额偏离: " + ", ".join(bad)
    return True, "8 容器限额=冻结值"


def _firewall_tokens(
    input_s: str, guard_s: str, rules: list[list[str]]
) -> tuple[bool, str]:
    """iptables -S 完整形态 token 全等（v4/v6 共用内核逻辑）。"""
    input_rules = [ln.split() for ln in input_s.splitlines() if ln.startswith("-A INPUT ")]
    if not input_rules:
        return False, "INPUT 无规则（跳转缺失）"
    if input_rules[0] != INPUT_JUMP:
        return False, f"INPUT 首条非无条件跳转（={input_rules[0]}）"
    guard_rules = [ln.split() for ln in guard_s.splitlines() if ln.startswith(f"-A {GUARD_CHAIN} ")]
    if len(guard_rules) != len(rules):
        return False, f"{GUARD_CHAIN} 规则数 {len(guard_rules)}≠{len(rules)}"
    for idx, (expect, got) in enumerate(zip(rules, guard_rules, strict=True), start=1):
        if expect != got:
            return False, f"规则{idx} 形态不等：期望 {' '.join(expect)} 实得 {' '.join(got)}"
    return True, f"{GUARD_CHAIN} 完整形态全等（{len(rules)} 条）"


def check_firewall_structure(
    input_s: str, guard_s: str
) -> tuple[bool, str]:
    """iptables（v4）-S 完整形态全等校验（R3：条件跳转/附加条件均拒绝）。

    - INPUT 第 1 条必须是**无条件**跳转（token 全等于
      ["-A","INPUT","-j",GUARD]）——带 -s/-d/-i/-o/-p 的条件跳转会让
      不匹配流量绕开守卫链（R3 复现缺陷）；
    - 链内规则逐条**完整 token 全等**：REJECT 带 -s 限缩、ACCEPT 带 -d
      限缩、多任何匹配项或动作修饰都视为结构漂移 → 拒绝。
    """
    return _firewall_tokens(input_s, guard_s, GUARD_RULES)


def check_firewall6(
    input6_result: tuple[int, str], guard6_result: tuple[int, str]
) -> tuple[bool, str]:
    """ip6tables 同构校验（R4）：[::]:9999 双栈监听 → v6 必须有等效保护。

    9999/8082 在 v6 侧由同一专用链保护：INPUT 首条无条件跳转 + 链内
    ::1 ACCEPT / 无条件 REJECT 完整形态全等；命令 rc≠0 一律拒绝。
    """
    for label, (rc, _out) in (("ip6tables", input6_result), ("ip6tables-guard", guard6_result)):
        if rc != 0:
            return False, f"{label} 命令失败 rc={rc}（拒绝，不解析输出）"
    return _firewall_tokens(input6_result[1], guard6_result[1], GUARD6_RULES)


def check_port_convergence(
    ss_result: tuple[int, str], input_result: tuple[int, str], guard_result: tuple[int, str]
) -> tuple[bool, str]:
    """监听收敛 + 防火墙结构（命令 rc≠0 一律拒绝）。"""
    for label, (rc, _out) in (("ss", ss_result), ("iptables", input_result), ("iptables-guard", guard_result)):
        if rc != 0:
            return False, f"{label} 命令失败 rc={rc}（拒绝，不解析输出）"
    problems: list[str] = []
    for port in LOOPBACK_BIND_PORTS:
        hits = [ln for ln in ss_result[1].splitlines() if f":{port} " in ln]
        if not hits:
            problems.append(f"{port}=未监听")
        elif not all(ln.split()[3].startswith("127.0.0.1:") for ln in hits):
            problems.append(f"{port}=非loopback")
    if problems:
        return False, "; ".join(problems)
    return check_firewall_structure(input_result[1], guard_result[1])


def check_egress_alive(connect: Callable[[str, int], bool]) -> tuple[bool, str]:
    """egress admin 9091 可达（静默退出检测兜底）。"""
    try:
        connected = connect("127.0.0.1", 9091)
    except OSError as exc:
        return False, f"egress 检查异常: {type(exc).__name__}"
    if connected:
        return True, "egress admin 9091 可达"
    return False, "egress admin 9091 不可达"


def run_all(
    toolbox: Path,
    *,
    read_file: Callable[[Path], str],
    docker_inspect: Callable[[str], tuple[int, str]],
    run_cmd: Callable[[list[str]], tuple[int, str]] = _run,
    connect: Callable[[str, int], bool] | None = None,
    manifest: Path | None = None,
    alarm_file: Path | None = None,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    results.append(("master_refs", *check_master_refs(read_file, toolbox)))
    results.append(("container_limits", *check_container_limits(docker_inspect)))
    results.append((
        "port_convergence",
        *check_port_convergence(
            run_cmd(["ss", "-tln"]),
            run_cmd(["iptables", "-S", "INPUT"]),
            run_cmd(["iptables", "-S", GUARD_CHAIN]),
        ),
    ))
    results.append((
        "firewall_v6",
        *check_firewall6(
            run_cmd(["ip6tables", "-S", "INPUT"]),
            run_cmd(["ip6tables", "-S", GUARD_CHAIN]),
        ),
    ))

    def _connect(host: str, port: int) -> bool:
        with socket.create_connection((host, port), timeout=3):
            return True

    results.append(("egress_alive", *check_egress_alive(connect or _connect)))
    if manifest is not None and alarm_file is not None:
        allowed, reason = check_gate(manifest, alarm_file)
        results.append(("gate", allowed, reason or "心跳门禁通过"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolbox", type=Path, default=Path("/usr/local/services/cubetoolbox"))
    parser.add_argument("--manifest", type=Path, default=Path("/root/cube-poc-supervisor/manifest.json"))
    parser.add_argument("--alarm-file", type=Path, default=Path("/root/cube-poc-supervisor/alarm"))
    args = parser.parse_args()

    results = run_all(
        args.toolbox,
        read_file=lambda p: p.read_text(),
        docker_inspect=lambda name: _run([
            "docker", "inspect", name, "--format", "{{.HostConfig.Memory}}",
        ]),
        manifest=args.manifest,
        alarm_file=args.alarm_file,
    )
    ok_all = True
    for name, ok, detail in results:
        print(f"PREFLIGHT {'PASS' if ok else 'FAIL'} {name}: {detail}")
        ok_all = ok_all and ok
    if not ok_all:
        raise SystemExit(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
