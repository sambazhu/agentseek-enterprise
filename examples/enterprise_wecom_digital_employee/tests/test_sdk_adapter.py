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

    def _send_raw(self, status_line: bytes, headers: bytes, body=b""):
        self.wfile.write(status_line + headers + body)

    def _drip_body(self, seconds: float = 5.0, interval: float = 0.05):
        """发完响应头后每 interval 发 1 字节，持续 seconds（慢滴流）。"""
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n")
        self.wfile.flush()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.wfile.write(b"x")
            self.wfile.flush()
            time.sleep(interval)

    def _send_raw(self, status_line: bytes, headers: bytes, body=b""):
        self.wfile.write(status_line + headers + body)

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.hits.append(("GET", self.path))
        if self.path == "/sandboxes":
            self._json(SANDBOXES)
        elif self.path == "/sandboxes?malformed=notalist":
            self._json({"unexpected": "object"})
        elif self.path == "/sandboxes/sbx-slow":
            time.sleep(30)  # 接受连接但不返回（静默挂起）
        elif self.path == "/sandboxes/sbx-drip":
            self._drip_body()  # 持续滴流（每次发送都小于 socket 超时）
        elif self.path == "/sandboxes/sbx-late-headers":
            time.sleep(2.0)  # 慢响应头
            self._json({"sandboxID": "sbx-late-headers", "startedAt": None, "metadata": {}})
        elif self.path == "/sandboxes/sbx-huge":
            self._send_raw(
                b"HTTP/1.1 200 OK\r\nContent-Length: 65536\r\n\r\n", b"", b"z" * 65536
            )
        elif self.path.startswith("/sandboxes/"):
            sid = self.path.rsplit("/", 1)[-1]
            info = INFOS.get(sid)
            self._json(info, 200) if info else self._json({"error": "not found"}, 404)
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
