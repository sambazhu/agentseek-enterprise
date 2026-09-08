"""PoC 创建前预检（.172 侧，失败关闭）——Codex R1 评审 Q6/Q7 新增门禁。

每次新建沙箱前与 gate-check 一并调用（exit 0=放行 / 3=阻断）。检查项：
1. master 活动引用不得回落 8089（dynamicconf cubemaster_http_addr、
   LCM compose CUBE_LCM_CUBEMASTER_URL 均须显式 127.0.0.1:18089）；
2. 容器限额核验：8 个容器 docker inspect Memory 必须等于冻结值——
   容器重建（compose up）后限额未核验不得创建（LCM 曾被重建冲掉）；
3. 端口收敛核验：ss 中 80/443/9090/9998/9966/8083 必须绑 127.0.0.1，
   9999/8082 必须有 iptables 源收敛规则（本机源 ACCEPT + 其他 REJECT）；
4. egress 存活：127.0.0.1:9091 可达（egress 曾静默退出 3 小时未自愈，
   RestartPolicy 已改 unless-stopped，此处为检测兜底）；
5. 监督心跳门禁（复用 check_gate：告警/manifest/心跳）。

全部读取器可注入（tests 用假内容），纯标准库。
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
EXPECTED_CONTAINER_MEMORY: dict[str, int] = {
    # MiB → 字节（§9.4 冻结值）
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
IPTABLES_GUARDED_PORTS = (9999, 8082)


def _run(cmd: list[str]) -> str:
    # 固定白名单命令（本模块自有 ss/iptables/docker inspect），无外部输入拼接
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)  # noqa: S603
    return proc.stdout + proc.stderr


def check_master_refs(read_file: Callable[[Path], str], toolbox: Path) -> tuple[bool, str]:
    """master 活动引用：dynamicconf + LCM compose 均须显式 18089（防 8089 回落）。"""
    problems: list[str] = []
    dyn = read_file(toolbox / "Cubelet/dynamicconf/conf.yaml")
    for line in dyn.splitlines():
        if "cubemaster_http_addr" in line:
            value = line.split(":", 1)[1].strip().strip('"')
            if value != EXPECTED_MASTER_ADDR:
                problems.append(f"dynamicconf={value!r}")
            break
    else:
        problems.append("dynamicconf cubemaster_http_addr 缺失")
    lcm = read_file(toolbox / "cube-lifecycle-manager/docker-compose.yaml")
    # 8089 残留检查用数字边界（"18089" 不算——朴素子串会恒真）
    has_8089 = re.search(r"(?<![0-9])8089(?![0-9])", lcm) is not None
    if (
        "CUBE_LCM_CUBEMASTER_URL" not in lcm
        or EXPECTED_MASTER_ADDR not in lcm
        or has_8089
    ):
        problems.append("LCM compose master URL 非 18089（或含 8089 残留）")
    if problems:
        return False, "; ".join(problems)
    return True, f"master 引用={EXPECTED_MASTER_ADDR}"


def check_container_limits(docker_inspect: Callable[[str], str]) -> tuple[bool, str]:
    """8 容器限额=冻结值（容器重建后未核验不得创建）。"""
    bad = []
    for name, mib in EXPECTED_CONTAINER_MEMORY.items():
        out = docker_inspect(name)
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


def check_port_convergence(ss_lines: str, iptables_list: str) -> tuple[bool, str]:
    """监听收敛：指定端口必须 127.0.0.1；9999/8082 必须有源收敛规则。"""
    problems: list[str] = []
    for port in LOOPBACK_BIND_PORTS:
        hits = [ln for ln in ss_lines.splitlines() if f":{port} " in ln]
        if not hits:
            problems.append(f"{port}=未监听")
        elif not all(ln.split()[3].startswith("127.0.0.1:") for ln in hits):
            problems.append(f"{port}=非loopback")
    for port in IPTABLES_GUARDED_PORTS:
        block = [ln for ln in iptables_list.splitlines() if f"dpt:{port}" in ln]
        has_accept = any("127.0.0.1" in ln and "ACCEPT" in ln for ln in block)
        has_reject = any("REJECT" in ln for ln in block)
        if not (has_accept and has_reject):
            problems.append(f"{port}=无iptables源收敛")
    if problems:
        return False, "; ".join(problems)
    return True, "端口收敛+iptables 规则在位"


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
    docker_inspect: Callable[[str], str],
    run_cmd: Callable[[list[str]], str] = _run,
    connect: Callable[[str, int], bool] | None = None,
    manifest: Path | None = None,
    alarm_file: Path | None = None,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    results.append(("master_refs", *check_master_refs(read_file, toolbox)))
    results.append(("container_limits", *check_container_limits(docker_inspect)))
    results.append((
        "port_convergence",
        *check_port_convergence(run_cmd(["ss", "-tln"]), run_cmd(["iptables", "-L", "INPUT", "-n"])),
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
