from __future__ import annotations

import io
import zipfile
from email.message import Message

import pytest
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_wecom.channel import (
    _extract_ai_bot_media,
    _extract_media_items,
    _media_decryption_key,
    _mixed_text_content,
)
from agentseek_wecom.media import (
    WeComMediaClient,
    decode_encoding_aes_key,
    decrypt_ai_bot_media,
    infer_media_extension,
)
from Crypto.Cipher import AES

AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


def test_extract_ai_bot_file_image_video_urls() -> None:
    file_media = _extract_ai_bot_media(
        {
            "msgtype": "file",
            "file": {"url": "https://ww-aibot-img.example.com/report.pdf?sign=secret"},
        }
    )
    image_media = _extract_ai_bot_media(
        {
            "msgtype": "image",
            "image": {"url": "https://ww-aibot-img.example.com/image.png?sign=secret"},
        }
    )
    video_media = _extract_ai_bot_media(
        {
            "msgtype": "video",
            "video": {"url": "https://ww-aibot-img.example.com/video.mp4?sign=secret"},
        }
    )

    assert file_media == {
        "url": "https://ww-aibot-img.example.com/report.pdf?sign=secret",
        "filename": "",
        "mime_type": "application/octet-stream",
        "kind": "file",
    }
    assert image_media and image_media["filename"] == ""
    assert image_media and image_media["mime_type"] == "image/jpeg"
    assert video_media and video_media["filename"] == ""
    assert video_media and video_media["mime_type"] == "video/mp4"


def test_long_connection_media_uses_per_url_aes_key() -> None:
    long_key = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"
    media = _extract_ai_bot_media(
        {
            "msgtype": "file",
            "file": {
                "url": "https://ww-aibot-img.example.com/opaque?sign=secret",
                "aeskey": long_key,
            },
        }
    )

    assert media is not None
    assert media["aes_key"] == long_key
    assert _media_decryption_key(media, callback_encoding_aes_key=AES_KEY) == decode_encoding_aes_key(long_key)


def test_ai_bot_download_uses_response_content_type_and_accepts_missing_filename(monkeypatch, tmp_path) -> None:
    key = decode_encoding_aes_key(AES_KEY)
    plaintext = b"%PDF-1.7\nmock document"
    response = _FakeResponse(_encrypt_for_test(plaintext, key), content_type="application/pdf; charset=binary")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    callback_media = _extract_ai_bot_media(
        {
            "msgtype": "file",
            "file": {"url": "https://ww-aibot-img.example.com/opaque-object?sign=secret"},
        }
    )
    assert callback_media is not None
    assert callback_media["filename"] == ""

    download = WeComMediaClient("", "").download_media(
        callback_media["url"],
        aes_key=key,
        fallback_filename=callback_media["filename"],
        fallback_mime_type=callback_media["mime_type"],
    )
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",)))
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename=download.filename,
        data=download.data,
        mime_type=download.mime_type,
    )

    assert download.mime_type == "application/pdf"
    assert download.filename.startswith("document_")
    assert download.filename.endswith(".pdf")
    assert record.sanitized_filename == download.filename


def test_ai_bot_download_replaces_hex_encoded_header_filename(monkeypatch) -> None:
    key = decode_encoding_aes_key(AES_KEY)
    response = _FakeResponse(
        _encrypt_for_test("测试内容".encode(), key),
        content_type="text/plain",
        content_disposition='attachment; filename="E6_B5_8B_E8_AF_95.txt"',
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    download = WeComMediaClient("", "").download_media(
        "https://ww-aibot-img.example.com/opaque?sign=secret",
        aes_key=key,
        fallback_filename="",
        fallback_mime_type="application/octet-stream",
    )

    assert download.filename.startswith("document_")
    assert download.filename.endswith(".txt")
    assert "E6_B5" not in download.filename


def test_ai_bot_download_replaces_hex_encoded_fallback_without_content_disposition(monkeypatch) -> None:
    key = decode_encoding_aes_key(AES_KEY)
    response = _FakeResponse(
        _encrypt_for_test("测试内容".encode(), key),
        content_type="text/plain",
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    download = WeComMediaClient("", "").download_media(
        "https://ww-aibot-img.example.com/opaque?sign=secret",
        aes_key=key,
        fallback_filename="E6_B5_8B_E8_AF_95.txt",
        fallback_mime_type="text/plain",
    )

    assert download.filename.startswith("document_")
    assert download.filename.endswith(".txt")
    assert "E6_B5" not in download.filename


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7", ".pdf"),
        (b"\xff\xd8\xff\xe0jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF89a", ".gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", ".webp"),
        (b"BMbitmap", ".bmp"),
    ],
)
def test_ai_bot_media_extension_falls_back_to_decrypted_magic_bytes(data: bytes, expected: str) -> None:
    assert infer_media_extension("application/octet-stream", data) == expected


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("application/msword", ".doc"),
        ("application/vnd.ms-excel", ".xls"),
        ("application/vnd.ms-powerpoint", ".ppt"),
    ],
)
def test_ai_bot_media_identifies_legacy_office_content_types(mime_type: str, expected: str) -> None:
    assert infer_media_extension(mime_type, b"legacy office") == expected


@pytest.mark.parametrize(
    ("member_name", "expected"),
    [("word/document.xml", ".docx"), ("ppt/presentation.xml", ".pptx"), ("xl/workbook.xml", ".xlsx")],
)
def test_ai_bot_media_identifies_ooxml_zip_container(member_name: str, expected: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, "test")

    assert infer_media_extension("application/octet-stream", buffer.getvalue()) == expected


def test_voice_content_is_plain_text_not_media() -> None:
    data = {"msgtype": "voice", "voice": {"content": "这是语音转写文本"}}

    assert _extract_media_items(data) == []


def test_mixed_splits_text_and_image_media() -> None:
    data = {
        "msgtype": "mixed",
        "mixed": {
            "msg_item": [
                {"msgtype": "text", "text": {"content": "@机器人 这是测试情况"}},
                {"msgtype": "image", "image": {"url": "https://ww-aibot-img.example.com/a.jpg?sign=x"}},
            ]
        },
    }

    assert _mixed_text_content(data) == "@机器人 这是测试情况"
    media_items = _extract_media_items(data)
    assert len(media_items) == 1
    assert media_items[0]["url"] == "https://ww-aibot-img.example.com/a.jpg?sign=x"
    assert media_items[0]["kind"] == "image"


def test_ai_bot_media_aes_256_cbc_decrypts_pkcs7_32_payload() -> None:
    key = decode_encoding_aes_key(AES_KEY)
    plaintext = b"hello decrypted ai bot file"
    encrypted = _encrypt_for_test(plaintext, key)

    assert decrypt_ai_bot_media(encrypted, aes_key=key) == plaintext


def _encrypt_for_test(value: bytes, key: bytes) -> bytes:
    pad_size = 32 - (len(value) % 32)
    padded = value + bytes([pad_size]) * pad_size
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return cipher.encrypt(padded)


class _FakeResponse:
    def __init__(self, data: bytes, *, content_type: str, content_disposition: str = "") -> None:
        self._data = data
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_disposition:
            self.headers["Content-Disposition"] = content_disposition

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data
