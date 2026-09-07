"""tunnel_forwarder 定向测试（v2）。

覆盖（对应 Codex 四次复核 F1/六）：
- 真实 CLI 子进程启动（单一事件循环修复后）+ TLS 正向请求→响应双向流；
- 错误 CA / 错误名称负向（连接必须失败）；
- 上游不可达失败关闭、断连清理（上游收到 EOF）；
- CLI 拒绝非 loopback 监听、无明文开关；
- 明文模式仅供测试经构造函数注入（require_tls=False）。

证书用 cryptography 在测试内临时生成（CA + 叶证书 SAN=127.0.0.1）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sandbox_poc.tunnel_forwarder import Forwarder

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
CLI = EXAMPLE_DIR / "sandbox_poc" / "tunnel_forwarder.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _now() -> datetime:
    return datetime.now(UTC)


def _make_ca(cn: str) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - timedelta(hours=1))
        .not_valid_after(_now() + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _make_leaf(ca_cert, ca_key, san_ip: str) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cube-data")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - timedelta(hours=1))
        .not_valid_after(_now() + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address(san_ip))]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


@pytest.fixture(scope="module")
def ca_pair(tmp_path_factory):
    """CA1（信任链）+ 叶证书 SAN=127.0.0.1 + CA2（错误信任负向用）。"""
    d = tmp_path_factory.mktemp("certs")
    ca1_cert, ca1_key = _make_ca("poc-ca-1")
    ca2_cert, _ = _make_ca("poc-ca-2")
    leaf_cert, leaf_key = _make_leaf(ca1_cert, ca1_key, "127.0.0.1")
    paths = {
        "ca1": d / "ca1.pem",
        "ca2": d / "ca2.pem",
        "leaf": d / "leaf.pem",
        "leaf_key": d / "leaf.key",
    }
    paths["ca1"].write_bytes(ca1_cert.public_bytes(serialization.Encoding.PEM))
    paths["ca2"].write_bytes(ca2_cert.public_bytes(serialization.Encoding.PEM))
    paths["leaf"].write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    paths["leaf_key"].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return paths


class TlsUpstream:
    """线程内 asyncio TLS 服务：记录收到的请求原文，回送固定响应。"""

    def __init__(self, cert: Path, key: Path) -> None:
        self.port = _free_port()
        self.received = b""
        self.eof_seen = threading.Event()
        self._loop = asyncio.new_event_loop()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self._ctx = ctx
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def handle(reader, writer):
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    self.received += chunk
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    await writer.drain()
            finally:
                self.eof_seen.set()
                writer.close()

        async def main():
            server = await asyncio.start_server(handle, "127.0.0.1", self.port, ssl=self._ctx)
            async with server:
                await server.serve_forever()

        asyncio.set_event_loop(self._loop)
        with contextlib.suppress(Exception):  # 线程随测试进程退出
            self._loop.run_until_complete(main())

    def start(self) -> TlsUpstream:
        self._thread.start()
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("tls upstream did not start")

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


def test_cli_subprocess_tls_roundtrip(ca_pair, tmp_path):
    """F1 回归：真实 CLI 子进程（单事件循环）+ TLS 正向 + 请求→响应双向流。"""
    upstream = TlsUpstream(ca_pair["leaf"], ca_pair["leaf_key"]).start()
    listen_port = _free_port()
    # 受信输入：sys.executable 执行本仓库内 CLI 文件。
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, str(CLI),
            "--listen-port", str(listen_port),
            "--upstream-host", "127.0.0.1", "--upstream-port", str(upstream.port),
            "--ca-file", str(ca_pair["ca1"]),
        ],
        cwd=EXAMPLE_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", listen_port), timeout=0.2):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError(f"CLI exited early: {proc.stdout.read()!r}") from None
                time.sleep(0.1)

        # 经转发器的 TLS 由转发器-上游之间完成；客户端→转发器为 loopback 明文。
        resp = asyncio.run(_connect_and_request(listen_port))
        assert resp == b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        request_line = b"GET /files HTTP/1.1\r\nHost: 49999-sbx.cube.app\r\n\r\n"
        for _ in range(50):
            if upstream.received.startswith(request_line[:20]):
                break
            time.sleep(0.1)
        assert upstream.received.startswith(request_line)  # Host 路由值透传
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        upstream.stop()


async def _connect_and_request(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /files HTTP/1.1\r\nHost: 49999-sbx.cube.app\r\n\r\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.read(4096), timeout=5)
    writer.close()  # 在协程内关闭（asyncio.run 结束后 loop 已关闭）
    return data


def test_wrong_ca_fails_closed(ca_pair):
    """负向：转发器只信任 CA1，上游用独立 rogue CA 签发的证书 → 必须失败关闭。"""
    d = ca_pair["ca1"].parent
    rogue_ca_cert, rogue_ca_key = _make_ca("rogue")
    rogue_leaf, rogue_key = _make_leaf(rogue_ca_cert, rogue_ca_key, "127.0.0.1")
    (d / "rogue.pem").write_bytes(rogue_leaf.public_bytes(serialization.Encoding.PEM))
    (d / "rogue.key").write_bytes(
        rogue_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    rogue_upstream = TlsUpstream(d / "rogue.pem", d / "rogue.key").start()

    async def scenario() -> bytes:
        fwd = Forwarder(
            "127.0.0.1", 0, "127.0.0.1", rogue_upstream.port,
            ca_file=str(ca_pair["ca1"]),  # 只信任 CA1 → rogue 链必须失败
        )
        await fwd.start()
        port = fwd._server.sockets[0].getsockname()[1]
        _r, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")
        await writer.drain()
        data = await asyncio.wait_for(_r.read(), timeout=5)
        await fwd.stop()
        return data

    assert asyncio.run(scenario()) == b""  # 失败关闭：无明文回退、无数据
    rogue_upstream.stop()


def test_wrong_server_name_fails_closed(ca_pair):
    """负向：server_hostname 与证书 SAN 不匹配 → 失败关闭。"""
    upstream = TlsUpstream(ca_pair["leaf"], ca_pair["leaf_key"]).start()

    async def scenario() -> bytes:
        fwd = Forwarder(
            "127.0.0.1", 0, "127.0.0.1", upstream.port,
            ca_file=str(ca_pair["ca1"]), server_hostname="wrong.example.internal",
        )
        await fwd.start()
        port = fwd._server.sockets[0].getsockname()[1]
        _r, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")
        await writer.drain()
        data = await asyncio.wait_for(_r.read(), timeout=5)
        await fwd.stop()
        return data

    assert asyncio.run(scenario()) == b""
    upstream.stop()


def test_cli_refuses_non_loopback_listen_host(tmp_path):
    # 断言 CLI 拒绝该绑定（0.0.0.0 仅为被测入参）。
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(CLI), "--listen-host", "0.0.0.0",  # noqa: S104
         "--ca-file", str(tmp_path / "any.pem")],
        cwd=EXAMPLE_DIR, capture_output=True, timeout=15,
    )
    assert proc.returncode != 0
    assert b"non-loopback" in proc.stdout + proc.stderr


def test_cli_has_no_plain_mode_flag(tmp_path):
    # 受信输入：本仓库内 CLI。
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(CLI), "--allow-plain"],
        cwd=EXAMPLE_DIR, capture_output=True, timeout=15,
    )
    assert proc.returncode != 0  # 无明文开关


def test_plain_mode_bidirectional_and_disconnect_cleanup():
    """明文模式（测试构造注入）：双向流 + 断连清理（上游收到 EOF）。

    注意：等待上游 EOF 必须用 asyncio.Event（threading.Event.wait 会阻塞
    事件循环本身，EOF 永远无法被处理）。
    """
    upstream_data = bytearray()

    async def scenario() -> tuple[bytes, bool]:
        upstream_eof = asyncio.Event()

        async def on_conn(reader, writer):
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    upstream_data.extend(chunk)
                    writer.write(b"PONG:" + chunk)
                    await writer.drain()
            finally:
                upstream_eof.set()
                writer.close()

        server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
        upstream_port = server.sockets[0].getsockname()[1]
        fwd = Forwarder("127.0.0.1", 0, "127.0.0.1", upstream_port, require_tls=False)
        await fwd.start()
        port = fwd._server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        await writer.wait_closed()
        eof_ok = await asyncio.wait_for(upstream_eof.wait(), timeout=5)
        server.close()
        await fwd.stop()
        return resp, eof_ok

    resp, eof_ok = asyncio.run(scenario())
    assert resp == b"PONG:ping"  # 双向：请求经转发、响应回流
    assert eof_ok  # 断连清理：客户端关闭 → 上游收到 EOF


def test_fail_closed_when_upstream_unreachable():
    async def scenario() -> bytes:
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        fwd = Forwarder("127.0.0.1", 0, "127.0.0.1", dead_port, require_tls=False)
        await fwd.start()
        port = fwd._server.sockets[0].getsockname()[1]
        _r, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")
        await writer.drain()
        data = await asyncio.wait_for(_r.read(), timeout=3)
        writer.close()
        await fwd.stop()
        return data

    assert asyncio.run(scenario()) == b""


def test_listener_defaults_to_loopback_and_tls_required():
    fwd = Forwarder("127.0.0.1", 13080, "192.10.50.172", 13080, ca_file="ca.pem")
    assert fwd.listen_host == "127.0.0.1"
    assert fwd.require_tls is True
