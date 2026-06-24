from __future__ import annotations

import json

import pytest

from agentseek_wecom.crypto import WeComCryptoError, WeComJsonCrypto, make_signature


AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


def test_crypto_encrypt_decrypt_roundtrip() -> None:
    crypto = WeComJsonCrypto(token="token", encoding_aes_key=AES_KEY, receive_id="")
    encrypted = crypto.encrypt_message('{"msgtype":"text","text":{"content":"你好"}}', nonce="nonce", timestamp="1")

    body = json.dumps({"encrypt": encrypted.encrypt})
    decrypted = crypto.decrypt_message(
        post_data=body,
        msg_signature=encrypted.msg_signature,
        timestamp="1",
        nonce="nonce",
    )

    assert json.loads(decrypted)["text"]["content"] == "你好"


def test_crypto_verify_url_returns_echo_plaintext() -> None:
    crypto = WeComJsonCrypto(token="token", encoding_aes_key=AES_KEY, receive_id="")
    encrypted = crypto.encrypt_message("echo-ok", nonce="nonce", timestamp="1")
    signature = make_signature("token", "1", "nonce", encrypted.encrypt)

    assert crypto.verify_url(
        msg_signature=signature,
        timestamp="1",
        nonce="nonce",
        echostr=encrypted.encrypt,
    ) == "echo-ok"


def test_crypto_rejects_bad_signature() -> None:
    crypto = WeComJsonCrypto(token="token", encoding_aes_key=AES_KEY, receive_id="")
    encrypted = crypto.encrypt_message("hello", nonce="nonce", timestamp="1")

    with pytest.raises(WeComCryptoError):
        crypto.decrypt_message(
            post_data=json.dumps({"encrypt": encrypted.encrypt}),
            msg_signature="bad",
            timestamp="1",
            nonce="nonce",
        )
