from __future__ import annotations

from agentseek_wecom.channel import _extract_ai_bot_media, _extract_media_items, _mixed_text_content
from agentseek_wecom.media import decode_encoding_aes_key, decrypt_ai_bot_media
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
        "filename": "report.pdf",
        "mime_type": "application/octet-stream",
        "kind": "file",
    }
    assert image_media and image_media["filename"] == "image.png"
    assert image_media and image_media["mime_type"] == "image/jpeg"
    assert video_media and video_media["filename"] == "video.mp4"
    assert video_media and video_media["mime_type"] == "video/mp4"


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
