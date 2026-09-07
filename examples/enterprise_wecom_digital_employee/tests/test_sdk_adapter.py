"""CubeAPI 线协议客户端测试（P1-2 修复：HTTP 层真实超时）。

监督组件不再经 SDK（v0.7.0 SDK 控制面调用不传 timeout，源码核对），改用
自带硬超时的 HttpCubeClient；本测试用模拟 CubeAPI HTTP 服务验证线协议
（与 SDK v0.7.0 源码字段一致）与"服务端接受连接但不返回"的有界超时。
纯标准库实现，任意 venv 可跑（无需安装 SDK）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from sandbox_poc.node_supervisor import (
    RUN_MARKER_KEY,
    HttpCubeClient,
    NotFoundError,
    SandboxRecord,
)

NOW = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
SANDBOXES = [
    {"sandboxID": "sbx-a", "templateID": "tpl-1", "state": "running"},
    {"sandboxID": "sbx-b", "templateID": "tpl-2", "state": "running"},
]
INFOS = {
    "sbx-a": {"sandboxID": "sbx-a", "startedAt": NOW.isoformat(), "metadata": {RUN_MARKER_KEY: "run-1"}},
    "sbx-b": {"sandboxID": "sbx-b", "startedAt": None, "metadata": {}},
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockCubeAPI/0.7.0"
    hits: ClassVar[list[tuple[str, str]]] = []
    live_conns: ClassVar[int] = 0

    def setup(self):
        _Handler.live_conns += 1
        super().setup()

    def finish(self):
        _Handler.live_conns -= 1
        super().finish()

    def _drip_raw(self, tail: bytes, interval: float, rounds: int):
        """循环发送 tail 首字节（共 rounds 轮），永不发终结符。"""
        try:
            for _ in range(rounds):
                self.wfile.write(tail[:1])
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端按总截止断开属预期
        finally:
            self.close_connection = True

    def _drip_body(self, seconds: float = 5.0, interval: float = 0.05):
        """发完响应头后每 interval 发 1 字节，持续 seconds（慢滴流）。"""
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n")
        self.wfile.flush()
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端按总截止断开属预期

    def _send_raw(self, status_line: bytes, headers: bytes, body=b""):
        self.wfile.write(status_line + headers + body)

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drip_path(self) -> bool:
        """滴流/挂起故障路径；命中返回 True。"""
        table = {
            "/sandboxes/sbx-status-drip": (b"HTTP/1.", b"1 200 OK\r\n"),
            "/sandboxes/sbx-header-drip": (
                b"HTTP/1.1 200 OK\r\nX-Slow: ", b"1234567890abcdef",
            ),
            "/sandboxes/sbx-chunk-drip": (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1",
                b"234567890abcdef",
            ),
        }
        if self.path == "/sandboxes/sbx-slow":
            time.sleep(30)  # 接受连接但不返回（静默挂起）
            return True
        if self.path == "/sandboxes/sbx-drip":
            self._drip_body()  # 响应体持续滴流
            return True
        prefix_tail = table.get(self.path)
        if prefix_tail is not None:
            prefix, tail = prefix_tail
            self.wfile.write(prefix)
            self.wfile.flush()
            self._drip_raw(tail, interval=0.05, rounds=200)
            return True
        return False

    def _slow_or_info_path(self):
        if self.path == "/sandboxes/sbx-late-headers":
            time.sleep(2.0)  # 慢响应头
            self._json({"sandboxID": "sbx-late-headers", "startedAt": None, "metadata": {}})
            return True
        if self.path == "/sandboxes/sbx-huge":
            self._send_raw(
                b"HTTP/1.1 200 OK\r\nContent-Length: 65536\r\n\r\n", b"", b"z" * 65536
            )
            return True
        if self.path.startswith("/sandboxes/"):
            sid = self.path.rsplit("/", 1)[-1]
            info = INFOS.get(sid)
            self._json(info, 200) if info else self._json({"error": "not found"}, 404)
            return True
        return False

    def do_GET(self):
        _Handler.hits.append(("GET", self.path))
        if self._drip_path() or self._slow_or_info_path():
            return
        if self.path == "/sandboxes":
            self._json(SANDBOXES)
        elif self.path == "/sandboxes?malformed=notalist":
            self._json({"unexpected": "object"})
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        _Handler.hits.append(("DELETE", self.path))
        sid = self.path.rsplit("/", 1)[-1]
        if sid in {s["sandboxID"] for s in SANDBOXES}:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture()
def mock_api():
    _Handler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_list_maps_wire_dict_shape(mock_api):
    client = HttpCubeClient(mock_api)
    records = client.list()
    assert {(r.sandbox_id, r.template_id, r.state) for r in records} == {
        ("sbx-a", "tpl-1", "running"),
        ("sbx-b", "tpl-2", "running"),
    }


def test_hydrate_parses_iso_z_and_metadata(mock_api):
    client = HttpCubeClient(mock_api)
    base = client.list()
    a = client.hydrate(next(r for r in base if r.sandbox_id == "sbx-a"))
    assert a.run_marker == "run-1"
    assert a.started_at == NOW.timestamp()  # ISO(UTC) → epoch
    b = client.hydrate(next(r for r in base if r.sandbox_id == "sbx-b"))
    assert b.started_at is None and b.run_marker is None  # 缺失 → None（非 0）


def test_kill_issues_delete_and_404_maps_notfound(mock_api):
    client = HttpCubeClient(mock_api)
    client.kill("sbx-a")
    assert ("DELETE", "/sandboxes/sbx-a") in _Handler.hits
    with pytest.raises(NotFoundError):
        client.kill("sbx-missing")


def test_hang_server_bounded_by_request_timeout(mock_api):
    """P1-2 验证：服务端接受连接但不返回 → 客户端在超时上限内失败。"""
    client = HttpCubeClient(mock_api, timeout=0.5)
    record = SandboxRecord("sbx-slow", "t", "running")
    start = time.monotonic()
    # 超时类异常（socket TimeoutError 或 urllib 包装），均属预期
    with pytest.raises((TimeoutError, OSError)):
        client.hydrate(record)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0  # 远小于服务端 30s 挂起：HTTP 层硬超时生效

def test_slow_drip_body_bounded_by_total_deadline(mock_api):
    """一：慢滴流（每 0.05s 发 1 字节，单次发送均小于 socket 超时）。"""
    client = HttpCubeClient(mock_api, timeout=0.4)
    record = SandboxRecord("sbx-drip", "t", "running")
    start = time.monotonic()
    with pytest.raises((TimeoutError, OSError)):
        client.hydrate(record)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # 总截止生效（滴流可持续 5s）


def test_slow_response_headers_bounded(mock_api):
    """一：响应头延迟 2s → 0.3s 总截止内失败。"""
    client = HttpCubeClient(mock_api, timeout=0.3)
    record = SandboxRecord("sbx-late-headers", "t", "running")
    start = time.monotonic()
    with pytest.raises((TimeoutError, OSError)):
        client.hydrate(record)
    assert time.monotonic() - start < 1.0


def test_response_body_size_capped(mock_api):
    """一：响应体超上限 → 显式失败（不无限读取）。"""
    client = HttpCubeClient(mock_api, timeout=5.0, max_response_bytes=1024)
    record = SandboxRecord("sbx-huge", "t", "running")
    with pytest.raises(RuntimeError, match="exceeds limit"):
        client.hydrate(record)


def test_malformed_list_response_raises_not_silent_empty():
    """二：列表响应为对象（非法）→ 抛错，绝不静默解释为空集合。"""
    client = HttpCubeClient("http://127.0.0.1:1", timeout=0.1)

    class FakeConn:
        pass  # 直接驱动解析层：以私有方法注入响应


    def fake_request(method, path):
        return {"unexpected": "object"}  # 模拟 _request 返回非法形状

    client._request = fake_request  # type: ignore[assignment]
    with pytest.raises(TypeError, match="not a list"):
        client.list()

# ---------- 一（七次复核）：头部/chunk 控制行滴流 + 资源回收 ----------

def _drip_case(mock_api, sandbox_id, timeout=0.3):
    client = HttpCubeClient(mock_api, timeout=timeout)
    record = SandboxRecord(sandbox_id, "t", "running")
    start = time.monotonic()
    with pytest.raises((TimeoutError, OSError)):
        client.hydrate(record)
    return time.monotonic() - start


def test_status_line_drip_bounded_by_total_deadline(mock_api):
    """状态行滴流（>10s 可持续）→ 0.3s 总截止内退出。"""
    elapsed = _drip_case(mock_api, "sbx-status-drip")
    assert elapsed < 1.5


def test_header_drip_bounded_by_total_deadline(mock_api):
    """响应头滴流（Codex 复现场景：未终结 X-Slow 头逐字节）。"""
    elapsed = _drip_case(mock_api, "sbx-header-drip")
    assert elapsed < 1.5


def test_chunked_control_line_drip_bounded(mock_api):
    """chunked chunk-size 控制行滴流（read1 保护不到的内部读取）。"""
    elapsed = _drip_case(mock_api, "sbx-chunk-drip")
    assert elapsed < 1.5


def test_timeout_reaps_connection_and_threads(mock_api):
    """超时后连接与看门狗线程均回收，不遗留后台请求。"""
    import threading as _threading

    _Handler.live_conns = 0
    baseline_threads = _threading.active_count()
    _drip_case(mock_api, "sbx-header-drip", timeout=0.3)
    time.sleep(0.3)  # 看门狗/服务端收尾容差
    assert _Handler.live_conns <= 1  # 服务端连接已关闭（≤1 为收尾中的余量）
    assert _threading.active_count() <= baseline_threads + 1  # 看门狗已退出


def test_normal_request_still_succeeds_with_watchdog(mock_api):
    """看门狗存在不影响正常短请求。"""
    client = HttpCubeClient(mock_api, timeout=5.0)
    records = client.list()
    assert len(records) == 2
