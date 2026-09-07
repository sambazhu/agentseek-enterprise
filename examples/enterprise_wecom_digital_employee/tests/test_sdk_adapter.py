"""真实 SDK 适配测试（F2）：cubesandbox==0.7.0 + 模拟 CubeAPI HTTP 服务。

需在装有 SDK 的环境运行（其余测试不依赖）：
  /path/to/venv-with-sdk/bin/python -m pytest tests/test_sdk_adapter.py
未安装 SDK 时跳过（importorskip），不阻塞常规门禁。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

pytest.importorskip("cubesandbox", reason="SDK 适配测试需 cubesandbox==0.7.0")

from sandbox_poc.node_supervisor import (
    RUN_MARKER_KEY,
    NotFoundError,
    build_sdk_client,
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


@pytest.fixture(scope="module")
def mock_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_list_maps_real_dict_shape(mock_api):
    client = build_sdk_client(mock_api)
    records = client.list()
    assert {(r.sandbox_id, r.template_id, r.state) for r in records} == {
        ("sbx-a", "tpl-1", "running"),
        ("sbx-b", "tpl-2", "running"),
    }


def test_hydrate_reads_started_at_and_marker(mock_api):
    client = build_sdk_client(mock_api)
    base = client.list()
    a = client.hydrate(next(r for r in base if r.sandbox_id == "sbx-a"))
    assert a.run_marker == "run-1"
    assert a.started_at == NOW.timestamp()  # ISO → epoch
    b = client.hydrate(next(r for r in base if r.sandbox_id == "sbx-b"))
    assert b.started_at is None and b.run_marker is None  # 缺失不得默认 0


def test_kill_issues_delete_and_404_maps_notfound(mock_api):
    client = build_sdk_client(mock_api)
    client.kill("sbx-a")
    assert ("DELETE", "/sandboxes/sbx-a") in _Handler.hits
    with pytest.raises(NotFoundError):
        client.kill("sbx-missing")
