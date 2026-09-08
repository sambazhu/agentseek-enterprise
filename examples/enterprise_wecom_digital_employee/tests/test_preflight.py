"""preflight（R1 评审 Q6/Q7 新门禁）定向测试：读取器注入、负向、聚合。"""

from __future__ import annotations

from pathlib import Path

from sandbox_poc.preflight import (
    check_container_limits,
    check_egress_alive,
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
GOOD_IPT = "\n".join([
    "7  ACCEPT  tcp  127.0.0.1  0.0.0.0/0  tcp dpt:9999",
    "8  ACCEPT  tcp  192.10.50.172  0.0.0.0/0  tcp dpt:9999",
    "9  REJECT  tcp  0.0.0.0/0  0.0.0.0/0  tcp dpt:9999 reject-with tcp-reset",
    "10 ACCEPT  tcp  127.0.0.1  0.0.0.0/0  tcp dpt:8082",
    "12 REJECT  tcp  0.0.0.0/0  0.0.0.0/0  tcp dpt:8082 reject-with tcp-reset",
])


def _read(files: dict[str, str]):
    def reader(p: Path) -> str:
        return files[str(p)]
    return reader


def test_master_refs_good():
    files = {
        "/t/Cubelet/dynamicconf/conf.yaml": GOOD_DYN,
        "/t/cube-lifecycle-manager/docker-compose.yaml": GOOD_LCM,
    }
    ok, detail = check_master_refs(_read(files), Path("/t"))
    assert ok, detail


def test_master_refs_rejects_empty_and_8089_fallback():
    for dyn, lcm, why in [
        ("  cubemaster_http_addr: \"\"\n", GOOD_LCM, "空值回落编译默认 8089"),
        (GOOD_DYN.replace("18089", "8089"), GOOD_LCM, "显式 8089"),
        ("no such key\n", GOOD_LCM, "键缺失"),
        (GOOD_DYN, GOOD_LCM.replace("http://127.0.0.1:18089", "http://192.10.50.172:8089"), "LCM 残留"),
        (GOOD_DYN, GOOD_LCM.replace("CUBE_LCM_CUBEMASTER_URL", "CUBE_LCM_OTHER"), "LCM 键缺失"),
    ]:
        files = {
            "/t/Cubelet/dynamicconf/conf.yaml": dyn,
            "/t/cube-lifecycle-manager/docker-compose.yaml": lcm,
        }
        ok, detail = check_master_refs(_read(files), Path("/t"))
        assert not ok, (why, detail)


def test_container_limits_exact_and_drift():
    def inspect_exact(name: str) -> str:
        from sandbox_poc.preflight import EXPECTED_CONTAINER_MEMORY
        return str(EXPECTED_CONTAINER_MEMORY[name] * 1024 * 1024) + "\n"

    ok, _ = check_container_limits(inspect_exact)
    assert ok

    def inspect_drift(name: str) -> str:
        return "0\n" if name == "cube-lifecycle-manager" else inspect_exact(name)

    ok, detail = check_container_limits(inspect_drift)
    assert not ok and "cube-lifecycle-manager" in detail  # 容器重建冲掉限额的实测场景


def test_port_convergence_matrix():
    ok, detail = check_port_convergence(GOOD_SS, GOOD_IPT)
    assert ok, detail
    # 非法绑定（80 外露）必须拦截
    bad_ss = GOOD_SS.replace("127.0.0.1:80 ", "0.0.0.0:80 ")
    ok, detail = check_port_convergence(bad_ss, GOOD_IPT)
    assert not ok and "80=非loopback" in detail
    # 缺 iptables 源收敛
    ok, detail = check_port_convergence(GOOD_SS, "")
    assert not ok and "9999=无iptables源收敛" in detail


def test_egress_alive_detects_down():
    assert check_egress_alive(lambda h, p: True)[0]
    ok, detail = check_egress_alive(lambda h, p: (_ for _ in ()).throw(OSError("refused")))
    assert not ok and "不可达" not in detail  # 异常路径也要失败关闭
    assert not check_egress_alive(lambda h, p: False)[0]
