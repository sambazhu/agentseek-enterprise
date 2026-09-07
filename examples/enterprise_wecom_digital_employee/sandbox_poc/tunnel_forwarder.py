"""PoC 数据面本地转发器（.171 侧，仅 loopback）。

职责（对应 V0.1.3_M0_INSTALL_CHANGE_LIST.md §7）：
- 监听 127.0.0.1:<port>，把字节流原样经 TLS 转发到 .172 数据面入口；
- 字节透传：HTTP Host（沙箱路由值）与 TrafficAccessToken 等头不被解析、
  不被改写、不被记录（仅时间戳与字节数）；
- 流式：按 chunk 双向转发，无请求/响应缓冲；
- 失败关闭：上游 TLS 握手失败/断开 -> 立即关闭客户端连接，无明文回退；
- 仅在 PoC 期间运行，随 PoC 启停，不安装系统包、不触碰现网服务。

测试（test_tunnel_forwarder.py）覆盖：字节透明（含 Host 头）、上游不可达
失败关闭、仅 loopback 监听。TLS 证书校验的活体负向在安装轮执行。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import ssl
import time

LOG = logging.getLogger("cube_poc_forwarder")


class Forwarder:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        upstream_host: str,
        upstream_port: int,
        *,
        ca_file: str | None = None,
        server_hostname: str | None = None,
        require_tls: bool = True,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.ca_file = ca_file
        self.server_hostname = server_hostname or upstream_host
        self.require_tls = require_tls
        self._server: asyncio.AbstractServer | None = None

    def _ssl_context(self) -> ssl.SSLContext:
        # CA 信任（非公钥 pin）：仅信任本轮内部 CA 签发的证书。
        ctx = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=self.ca_file
        )
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
        total = 0
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                total += len(chunk)
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()
        return total

    async def _handle(self, c_reader, c_writer) -> None:
        started = time.monotonic()
        sent = recv = 0
        try:
            if self.require_tls:
                ctx = self._ssl_context()
                u_reader, u_writer = await asyncio.open_connection(
                    self.upstream_host,
                    self.upstream_port,
                    ssl=ctx,
                    server_hostname=self.server_hostname,
                )
            else:
                u_reader, u_writer = await asyncio.open_connection(
                    self.upstream_host, self.upstream_port
                )
        except Exception as exc:  # 失败关闭：不回退明文，直接断开
            LOG.warning("upstream connect failed (fail-closed): %s", type(exc).__name__)
            c_writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await c_writer.wait_closed()
            return
        sent, recv = await asyncio.gather(
            self._pipe(c_reader, u_writer), self._pipe(u_reader, c_writer)
        )
        LOG.info(
            "relayed bytes=%d/%d elapsed=%.1fs (no headers logged)",
            sent,
            recv,
            time.monotonic() - started,
        )

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )
        LOG.info(
            "listening on %s:%d -> %s:%d (tls=%s)",
            self.listen_host,
            self.listen_port,
            self.upstream_host,
            self.upstream_port,
            self.require_tls,
        )

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=13080)
    parser.add_argument("--upstream-host", default="192.10.50.172")
    parser.add_argument("--upstream-port", type=int, default=13080)
    parser.add_argument("--ca-file", required=True, help="内部 CA 证书路径")
    parser.add_argument(
        "--server-hostname",
        help="TLS 名称校验值（默认=upstream host；须与证书 SAN 一致）",
    )
    parser.add_argument("--allow-plain", action="store_true", help="仅限测试")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    forwarder = Forwarder(
        args.listen_host,
        args.listen_port,
        args.upstream_host,
        args.upstream_port,
        ca_file=args.ca_file,
        server_hostname=args.server_hostname,
        require_tls=not args.allow_plain,
    )
    asyncio.run(forwarder.start())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(forwarder.serve_forever())


if __name__ == "__main__":
    main()
