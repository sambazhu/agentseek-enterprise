"""PoC 创建前预检（.172 侧，失败关闭）——R2 评审硬化版。

每次新建沙箱前与 gate-check 一并调用（exit 0=放行 / 3=阻断）。检查项：
1. master 活动引用：dynamicconf `cubemaster_http_addr` 与 LCM compose
   `CUBE_LCM_CUBEMASTER_URL` 的**活动值**（跳过注释行，多处活动出现视为
   歧义拒绝）必须精确等于 127.0.0.1:18089——注释里的正确地址不能掩盖
   活动值为空/错误（R2 复现缺陷）；
2. 容器限额：8 容器 docker inspect Memory 必须等于冻结值；
3. 端口收敛+防火墙结构：指定端口必须绑 127.0.0.1；9999/8082 必须由
   **专用链 CUBE_POC_GUARD** 保护——校验 INPUT 首条即跳转该链（前置
   放行绕过不可行）、链内规则**逐条按序**精确匹配（协议/来源/端口 token
   全等比较，`dpt:80820` 不会误认 `dpt:8082`）；**命令 returncode≠0 一律
   拒绝**（不解析输出就放行）；
4. egress 存活：127.0.0.1:9091 可达；
5. 监督心跳门禁（复用 check_gate）。

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
# 链内期望规则（有序，全等匹配）：(target, source, port)
GUARD_RULES: list[tuple[str, str, int]] = [
    ("ACCEPT", "127.0.0.1", 9999),
    ("ACCEPT", "192.10.50.172", 9999),
    ("REJECT", "0.0.0.0/0", 9999),
    ("ACCEPT", "127.0.0.1", 8082),
    ("ACCEPT", "192.10.50.172", 8082),
    ("REJECT", "0.0.0.0/0", 8082),
]
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


def _parse_chain_rules(iptables_lines: str) -> list[dict[str, str]]:
    """把 `iptables -L <chain> -n --line-numbers` 输出解析为规则 dict 列表。"""
    rules = []
    for line in iptables_lines.splitlines():
        tokens = line.split()
        # 行格式：num target prot opt source destination [extras...]
        if len(tokens) >= 6 and tokens[0].isdigit():
            extras = " ".join(tokens[6:])
            rules.append({
                "target": tokens[1], "prot": tokens[2],
                "source": tokens[4], "extras": extras,
            })
    return rules


def check_firewall_structure(
    input_listing: str, guard_listing: str
) -> tuple[bool, str]:
    """专用链结构校验：INPUT 首条=跳转 GUARD 链；链内规则逐条按序全等。

    - 前置全放行必然把跳转挤到非首位（或在其前放行目标端口）→ 拒绝；
    - 端口用 token 全等（"dpt:9999"），`dpt:80820`/`dpt:99991` 不误认；
    - 规则数量必须恰为期望序列（多出的目标端口规则=结构漂移）。
    """
    # INPUT 首条必须是跳转专用链（首条之前不存在任何可绕过规则）
    first = None
    for line in input_listing.splitlines():
        tokens = line.split()
        if len(tokens) >= 6 and tokens[0].isdigit():
            first = tokens
            break
    if first is None or first[1] != GUARD_CHAIN:
        return False, f"INPUT 首条非 {GUARD_CHAIN} 跳转（首条={first[1] if first else '无规则'}）"

    rules = _parse_chain_rules(guard_listing)
    if len(rules) != len(GUARD_RULES):
        return False, f"GUARD 链规则数 {len(rules)}≠{len(GUARD_RULES)}"
    for idx, ((target, source, port), rule) in enumerate(zip(GUARD_RULES, rules, strict=True), start=1):
        if rule["target"] != target or rule["source"] != source or rule["prot"] != "tcp":
            return False, f"规则{idx} 不匹配: {rule}"
        port_token = f"dpt:{port}"
        tokens = rule["extras"].split()
        if port_token not in tokens:
            return False, f"规则{idx} 端口 token 不匹配（期望 {port_token}）"
        if target == "REJECT" and "reject-with" not in rule["extras"] and "tcp-reset" not in rule["extras"]:
            return False, f"规则{idx} REJECT 缺 tcp-reset"
    return True, f"{GUARD_CHAIN} 链结构=期望序列（{len(GUARD_RULES)} 条）"


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
            run_cmd(["iptables", "-L", "INPUT", "-n", "--line-numbers"]),
            run_cmd(["iptables", "-L", GUARD_CHAIN, "-n", "--line-numbers"]),
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
