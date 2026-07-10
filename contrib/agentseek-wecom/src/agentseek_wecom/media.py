from __future__ import annotations

import io
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES

from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComCryptoError

_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "audio/amr": ".amr",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/json": ".json",
    "text/csv": ".csv",
    "text/markdown": ".md",
}

_EXTENSION_CONTENT_TYPES = {
    extension: content_type for content_type, extension in reversed(_CONTENT_TYPE_EXTENSIONS.items())
}
_EXTENSION_CONTENT_TYPES[".jpg"] = "image/jpeg"
_EXTENSION_CONTENT_TYPES[".mp3"] = "audio/mpeg"
_EXTENSION_CONTENT_TYPES[".wav"] = "audio/wav"


@dataclass
class MediaDownload:
    media_id: str
    data: bytes
    filename: str
    mime_type: str


@dataclass
class _AccessToken:
    value: str
    expires_at: float


@dataclass
class WeComMediaClient:
    """Download media for WeCom intelligent robots.

    AI Bot callbacks provide signed temporary download URLs. The downloaded
    bytes are encrypted with the same EncodingAESKey as callback messages. The
    legacy self-built app ``media/get`` API is kept for compatibility, but the
    enterprise-wecom runtime uses ``download_media`` for AI Bot callbacks.
    """

    corp_id: str
    app_secret: str
    api_base_url: str = "https://qyapi.weixin.qq.com"
    timeout_seconds: float = 10.0
    _access_token: _AccessToken | None = field(default=None, init=False)

    @classmethod
    def from_settings(cls, settings: WeComSettings) -> WeComMediaClient:
        return cls(
            corp_id=settings.corp_id,
            app_secret=settings.app_secret,
            api_base_url=settings.api_base_url,
            timeout_seconds=settings.api_timeout_seconds,
        )

    @property
    def _base_url(self) -> str:
        base_url = self.api_base_url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("WeCom API base URL must be http(s)")
        return base_url

    def download(self, media_id: str, *, fallback_filename: str, fallback_mime_type: str) -> MediaDownload:
        """Download legacy self-built app temporary media by media_id."""
        media_id = str(media_id or "").strip()
        if not media_id:
            raise ValueError("media_id is required")
        token = self._get_access_token()
        query = urllib.parse.urlencode({"access_token": token, "media_id": media_id})
        request = urllib.request.Request(  # noqa: S310 - URL is validated to http(s) in _base_url.
            f"{self._base_url}/cgi-bin/media/get?{query}",
            headers={"Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                data = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"media/get failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"media/get network error: {exc}") from exc

        content_type = headers.get_content_type() or fallback_mime_type
        if content_type == "application/json":
            error = _json_error(data)
            if error is not None:
                raise RuntimeError(
                    f"media/get failed: errcode={error.get('errcode')} errmsg={error.get('errmsg')}"
                )

        filename = _filename_from_headers(headers) or fallback_filename
        mime_type = content_type or fallback_mime_type or _guess_mime_type(filename)
        return MediaDownload(media_id=media_id, data=data, filename=filename, mime_type=mime_type)

    def download_media(
        self,
        url: str,
        *,
        aes_key: bytes,
        fallback_filename: str,
        fallback_mime_type: str,
    ) -> MediaDownload:
        """Download and decrypt an AI Bot signed media URL."""
        safe_url = _validate_signed_media_url(url)
        request = urllib.request.Request(  # noqa: S310 - URL is validated to http(s).
            safe_url,
            headers={"Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                encrypted = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"aibot media download failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"aibot media download network error: {exc}") from exc

        if headers.get_content_type() == "application/json":
            error = _json_error(encrypted)
            if error is not None:
                raise RuntimeError(
                    f"aibot media download failed: errcode={error.get('errcode')} errmsg={error.get('errmsg')}"
                )

        plaintext = decrypt_ai_bot_media(encrypted, aes_key=aes_key)
        filename, mime_type = _resolve_ai_bot_media_metadata(
            headers,
            plaintext,
            fallback_filename=fallback_filename,
            fallback_mime_type=fallback_mime_type,
        )
        return MediaDownload(media_id=_redacted_media_source(safe_url), data=plaintext, filename=filename, mime_type=mime_type)

    def _get_access_token(self) -> str:
        if not self.corp_id or not self.app_secret:
            raise RuntimeError("legacy media/get requires WeCom corp_id and app_secret")
        now = time.time()
        if self._access_token is not None and now < self._access_token.expires_at:
            return self._access_token.value

        query = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.app_secret})
        request = urllib.request.Request(  # noqa: S310 - URL is validated to http(s) in _base_url.
            f"{self._base_url}/cgi-bin/gettoken?{query}",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"gettoken failed with HTTP {exc.code}: {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"gettoken network error: {exc}") from exc

        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("gettoken response missing access_token")
        expires_in = int(data.get("expires_in") or 7200)
        self._access_token = _AccessToken(token, now + max(expires_in - 120, 60))
        return token


def _filename_from_headers(headers: Message) -> str | None:
    disposition = headers.get("Content-Disposition") or ""
    if not disposition:
        return None
    message = Message()
    message["Content-Disposition"] = disposition
    params = dict(message.get_params(header="Content-Disposition") or [])
    value = params.get("filename") or params.get("filename*")
    return str(value).strip() if value else None


def _guess_mime_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def infer_media_extension(mime_type: str, data: bytes) -> str:
    """Infer a decrypted AI Bot media extension from HTTP metadata, then magic bytes."""
    normalized_mime_type = _normalize_mime_type(mime_type)
    if normalized_mime_type in _CONTENT_TYPE_EXTENSIONS:
        return _CONTENT_TYPE_EXTENSIONS[normalized_mime_type]
    return _extension_from_magic_bytes(data)


def _resolve_ai_bot_media_metadata(
    headers: Message,
    data: bytes,
    *,
    fallback_filename: str,
    fallback_mime_type: str,
) -> tuple[str, str]:
    header_mime_type = _header_content_type(headers)
    extension = infer_media_extension(header_mime_type, data)

    if extension == ".bin":
        fallback_extension = _CONTENT_TYPE_EXTENSIONS.get(_normalize_mime_type(fallback_mime_type))
        if fallback_extension:
            extension = fallback_extension

    preferred_filename = _filename_from_headers(headers) or str(fallback_filename or "").strip()
    preferred_extension = Path(preferred_filename).suffix.lower()
    if extension == ".bin" and preferred_extension in _EXTENSION_CONTENT_TYPES:
        extension = preferred_extension

    if header_mime_type in _CONTENT_TYPE_EXTENSIONS:
        mime_type = header_mime_type
    else:
        mime_type = _EXTENSION_CONTENT_TYPES.get(extension, _normalize_mime_type(fallback_mime_type))
    if not mime_type:
        mime_type = "application/octet-stream"

    filename = _filename_with_extension(preferred_filename, extension, mime_type)
    return filename, mime_type


def _header_content_type(headers: Message) -> str:
    if not headers.get("Content-Type"):
        return ""
    return _normalize_mime_type(headers.get_content_type())


def _normalize_mime_type(mime_type: str) -> str:
    return str(mime_type or "").partition(";")[0].strip().lower()


def _extension_from_magic_bytes(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"PK\x03\x04"):
        return _ooxml_extension(data)
    return ".bin"


def _ooxml_extension(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return ".bin"
    if any(name.startswith("word/") for name in names):
        return ".docx"
    if any(name.startswith("ppt/") for name in names):
        return ".pptx"
    if any(name.startswith("xl/") for name in names):
        return ".xlsx"
    return ".bin"


def _filename_with_extension(filename: str, extension: str, mime_type: str) -> str:
    clean_name = Path(filename).name.strip() if filename else ""
    current_extension = Path(clean_name).suffix
    if clean_name and current_extension.lower() != ".bin":
        if extension == ".bin" or current_extension.lower() == extension:
            return clean_name
        return f"{clean_name[: -len(current_extension)] if current_extension else clean_name}{extension}"

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    prefix = _friendly_media_prefix(extension, mime_type)
    return f"{prefix}_{timestamp}{extension}"


def _friendly_media_prefix(extension: str, mime_type: str) -> str:
    if extension in {".jpg", ".png", ".gif", ".webp", ".bmp"} or mime_type.startswith("image/"):
        return "image"
    if extension in {".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".pptx", ".xlsx"}:
        return "document"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    return "file"


def decode_encoding_aes_key(encoding_aes_key: str) -> bytes:
    try:
        import base64

        key = base64.b64decode(f"{encoding_aes_key.strip()}=")
    except Exception as exc:
        raise WeComCryptoError("Invalid EncodingAESKey") from exc
    if len(key) != 32:
        raise WeComCryptoError("Invalid EncodingAESKey length")
    return key


def decrypt_ai_bot_media(encrypted: bytes, *, aes_key: bytes) -> bytes:
    if len(aes_key) != 32:
        raise WeComCryptoError("Invalid EncodingAESKey length")
    if not encrypted or len(encrypted) % AES.block_size != 0:
        raise WeComCryptoError("Invalid encrypted media payload length")
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
    plaintext = cipher.decrypt(encrypted)
    return _pkcs7_unpad_32(plaintext)


def _pkcs7_unpad_32(value: bytes) -> bytes:
    if not value:
        raise WeComCryptoError("Empty decrypted media payload")
    pad_size = value[-1]
    if pad_size < 1 or pad_size > 32:
        raise WeComCryptoError("Invalid decrypted media payload padding")
    if value[-pad_size:] != bytes([pad_size]) * pad_size:
        raise WeComCryptoError("Invalid decrypted media payload padding bytes")
    return value[:-pad_size]


def _validate_signed_media_url(url: str) -> str:
    safe_url = str(url or "").strip()
    parsed = urllib.parse.urlparse(safe_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("AI Bot media URL must be http(s)")
    return safe_url


def _redacted_media_source(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _json_error(data: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and "errcode" in parsed:
        return parsed
    return None
