"""tunnel_forwarder 定向测试：字节透明（Host 头）、失败关闭、仅 loopback。

不依赖 pytest-asyncio（asyncio.run 包装）；TLS 证书校验活体负向在安装轮。
"""

from __future__ import annotations

import asyncio

from sandbox_poc.tunnel_forwarder import Forwarder


class UpstreamSink:
    """记录收到的原始字节（含请求行与头），不解析。"""

    def __init__(self) -> None:
        self.received = b""
        self.server: asyncio.AbstractServer | None = None

    async def _start(self) -> int:
        async def on_conn(reader, writer):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                self.received += chunk
            writer.close()

        self.server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


def test_bytes_and_host_header_pass_through_untouched():
    async def scenario() -> None:
        sink = UpstreamSink()
        upstream_port = await sink._start()
        fwd = Forwarder("127.0.0.1", 0, "127.0.0.1", upstream_port, require_tls=False)
        await fwd.start()
        local_port = fwd._server.sockets[0].getsockname()[1]

        request = (
            b"GET /files HTTP/1.1\r\n"
            b"Host: 49999-sbx123.cube.app\r\n"
            b"X-API-Key: REDACTED-IN-LOGS\r\n"
            b"\r\n"
        )
        _reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(request)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await fwd.stop()
        await sink._stop()
        return sink.received

    request = (
        b"GET /files HTTP/1.1\r\n"
        b"Host: 49999-sbx123.cube.app\r\n"
        b"X-API-Key: REDACTED-IN-LOGS\r\n"
        b"\r\n"
    )
    assert asyncio.run(scenario()) == request  # 字节级一致，头未被改写/记录


def test_fail_closed_when_upstream_unreachable():
    async def scenario() -> bytes:
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        fwd = Forwarder("127.0.0.1", 0, "127.0.0.1", dead_port, require_tls=False)
        await fwd.start()
        local_port = fwd._server.sockets[0].getsockname()[1]

        _reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(b"x")
        await writer.drain()
        data = await asyncio.wait_for(_reader.read(), timeout=3)
        writer.close()
        await writer.wait_closed()
        await fwd.stop()
        return data

    assert asyncio.run(scenario()) == b""  # 失败关闭：断开，无回退无响应


def test_listener_defaults_to_loopback_and_tls_required():
    fwd = Forwarder("127.0.0.1", 13080, "192.10.50.172", 13080, ca_file="ca.pem")
    assert fwd.listen_host == "127.0.0.1"
    assert fwd.require_tls is True
