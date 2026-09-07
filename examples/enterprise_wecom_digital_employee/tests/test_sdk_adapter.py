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
        elif self.path == "/sandboxes/sbx-slow":
            time.sleep(30)  # 接受连接但不返回（P1-2 场景）
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
