from datetime import datetime, timedelta, timezone
import ipaddress
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from agentseek_execution.business_http import BusinessHttpClient, make_tls_server, validated_result
from test_business_broker import broker_case


@pytest.fixture
def tls_broker(broker_case):
    s = broker_case
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-loopback")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256()))
    ca, private = s.path / "synthetic-cert.pem", s.path / "synthetic-key.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM)); ca.chmod(0o600)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())); private.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    server = make_tls_server(("127.0.0.1", 0), tls_context=context, broker=s.broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield s, f"https://127.0.0.1:{server.server_port}", ca
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert not thread.is_alive()


def test_real_loopback_tls_submit_and_read_only_recovery(tls_broker):
    s, endpoint, ca = tls_broker
    client = BusinessHttpClient(endpoint=endpoint, token=s.token, ca_file=ca)
    result = client.exchange(s.request, data=s.data)
    outcome, data = validated_result(s.request, result)
    assert outcome.state == "succeeded" and data == b"group,total\nA,3\nB,4\n"
    assert client.exchange(s.request) == result
    assert s.provider.events.count("create") == 1


def test_bad_token_does_not_reach_provider(tls_broker):
    s, endpoint, ca = tls_broker
    client = BusinessHttpClient(endpoint=endpoint, token="incorrect"*8, ca_file=ca)
    with pytest.raises(ValueError): client.exchange(s.request, data=s.data)
    assert not s.provider.events


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:1234", "https://user:pass@example.invalid", "https://example.invalid/path"])
def test_client_requires_dedicated_https_origin(endpoint):
    with pytest.raises(ValueError): BusinessHttpClient(endpoint=endpoint, token="x"*32, ca_file="unused")
