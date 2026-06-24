from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import time
from dataclasses import dataclass

from Crypto.Cipher import AES


class WeComCryptoError(ValueError):
    """Raised when WeCom callback crypto validation fails."""


@dataclass(frozen=True)
class EncryptedMessage:
    encrypt: str
    msg_signature: str
    timestamp: str
    nonce: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "encrypt": self.encrypt,
                "msgsignature": self.msg_signature,
                "timestamp": self.timestamp,
                "nonce": self.nonce,
            },
            ensure_ascii=False,
        )


class WeComJsonCrypto:
    """JSON callback crypto compatible with Enterprise WeChat intelligent robot callbacks."""

    def __init__(self, *, token: str, encoding_aes_key: str, receive_id: str = "") -> None:
        self.token = token
        self.receive_id = receive_id
        try:
            self.key = base64.b64decode(f"{encoding_aes_key}=")
        except Exception as exc:
            raise WeComCryptoError("Invalid EncodingAESKey") from exc
        if len(self.key) != 32:
            raise WeComCryptoError("Invalid EncodingAESKey length")

    def verify_url(self, *, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        self._assert_signature(msg_signature, timestamp, nonce, echostr)
        return self._decrypt_payload(echostr)

    def decrypt_message(self, *, post_data: bytes | str, msg_signature: str, timestamp: str, nonce: str) -> str:
        try:
            body = post_data.decode("utf-8") if isinstance(post_data, bytes) else post_data
            encrypt = str(json.loads(body)["encrypt"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WeComCryptoError("Invalid encrypted JSON body") from exc
        self._assert_signature(msg_signature, timestamp, nonce, encrypt)
        return self._decrypt_payload(encrypt)

    def encrypt_message(self, plain_text: str, *, nonce: str, timestamp: str | None = None) -> EncryptedMessage:
        timestamp = timestamp or str(int(time.time()))
        encrypt = self._encrypt_payload(plain_text)
        msg_signature = make_signature(self.token, timestamp, nonce, encrypt)
        return EncryptedMessage(
            encrypt=encrypt,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
        )

    def _assert_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        expected = make_signature(self.token, timestamp, nonce, encrypt)
        if expected != msg_signature:
            raise WeComCryptoError("Invalid WeCom message signature")

    def _encrypt_payload(self, plain_text: str) -> str:
        payload = plain_text.encode("utf-8")
        receive_id = self.receive_id.encode("utf-8")
        raw = secrets.token_bytes(16) + struct.pack("!I", len(payload)) + payload + receive_id
        padded = _pkcs7_pad(raw)
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")

    def _decrypt_payload(self, encrypt: str) -> str:
        try:
            cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
            plain = cipher.decrypt(base64.b64decode(encrypt))
            plain = _pkcs7_unpad(plain)
            content = plain[16:]
            json_len = socket.ntohl(struct.unpack("I", content[:4])[0])
            json_content = content[4 : json_len + 4].decode("utf-8")
            from_receive_id = content[json_len + 4 :].decode("utf-8")
        except Exception as exc:
            raise WeComCryptoError("Could not decrypt WeCom payload") from exc

        if from_receive_id != self.receive_id:
            raise WeComCryptoError("WeCom receive id mismatch")
        return json_content


def make_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    pieces = [str(token), str(timestamp), str(nonce), str(encrypt)]
    pieces.sort()
    return hashlib.sha1("".join(pieces).encode("utf-8")).hexdigest()


def _pkcs7_pad(value: bytes, block_size: int = 32) -> bytes:
    pad_size = block_size - (len(value) % block_size)
    if pad_size == 0:
        pad_size = block_size
    return value + bytes([pad_size]) * pad_size


def _pkcs7_unpad(value: bytes, block_size: int = 32) -> bytes:
    if not value:
        raise WeComCryptoError("Empty decrypted payload")
    pad_size = value[-1]
    if pad_size < 1 or pad_size > block_size:
        raise WeComCryptoError("Invalid decrypted payload padding")
    return value[:-pad_size]
