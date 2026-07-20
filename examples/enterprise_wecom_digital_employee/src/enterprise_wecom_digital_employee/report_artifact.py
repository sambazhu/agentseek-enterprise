from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import uuid4
from xml.sax.saxutils import escape

from agentseek_work import WorkContractSnapshot

REPORT_ARTIFACT_FORMAT_DOCX = "docx"
REPORT_ARTIFACT_MEDIA_TYPE_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
REPORT_ARTIFACT_TYPE = "report"

_VERSION_PATTERNS = (
    re.compile(r"report\s*draft\s*(?:version|版本)?\s*[vV]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:报告初稿|初稿|报告草稿)\s*(?:version|版本|第)?\s*[vV]?\s*(\d+)\s*版?"),
)
_RENDER_ACTION_RE = re.compile(
    r"(?:生成|渲染|导出|制作|创建).{0,18}(?:文件|文档|docx|word)|"
    r"(?:docx|word).{0,18}(?:生成|渲染|导出|制作|创建)|"
    r"\b(?:render|export|generate|create)\b.{0,24}\b(?:docx|word)\b",
    re.IGNORECASE,
)
_DOCX_RE = re.compile(r"\bdocx\b|\bword\b|Word\s*文档", re.IGNORECASE)
_NEGATED_RE = re.compile(
    r"(?:不|未|尚未|暂不|不要|不能|无需|别).{0,12}(?:生成|渲染|导出|制作|创建)|"
    r"\b(?:do\s+not|don't|not)\b.{0,16}\b(?:render|export|generate|create)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(?:是否|要不要|能否|可否).{0,24}(?:生成|渲染|导出|制作|创建)|"
    r"(?:生成|渲染|导出|制作|创建).{0,24}(?:吗|么)[？?]?\s*$",
    re.IGNORECASE,
)
_SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class ReportArtifactError(RuntimeError):
    """Raised when deterministic report rendering or storage fails closed."""


@dataclass(frozen=True, slots=True)
class StoredArtifactBlob:
    content_sha256: str
    size_bytes: int
    storage_key: str


class ContentAddressedArtifactStore:
    """Write immutable output blobs below one configured runtime root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, *, tenant_id: str, work_id: str, data: bytes, suffix: str) -> StoredArtifactBlob:
        if not data:
            raise ReportArtifactError("artifact data must not be empty")
        for value, field_name in ((tenant_id, "tenant_id"), (work_id, "work_id")):
            if not _SAFE_SCOPE_RE.fullmatch(value):
                raise ReportArtifactError(f"{field_name} is not safe for artifact storage")
        normalized_suffix = suffix.lower().lstrip(".")
        if normalized_suffix != REPORT_ARTIFACT_FORMAT_DOCX:
            raise ReportArtifactError("unsupported artifact format")
        content_hash = sha256(data).hexdigest()
        storage_key = PurePosixPath(
            tenant_id,
            work_id,
            normalized_suffix,
            content_hash[:2],
            f"{content_hash}.{normalized_suffix}",
        )
        target = self._resolve(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise ReportArtifactError("content-addressed artifact path contains different bytes")
        else:
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(data)
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return StoredArtifactBlob(
            content_sha256=f"sha256:{content_hash}",
            size_bytes=len(data),
            storage_key=storage_key.as_posix(),
        )

    def resolve(self, storage_key: str) -> Path:
        return self._resolve(PurePosixPath(storage_key))

    def _resolve(self, storage_key: PurePosixPath) -> Path:
        if storage_key.is_absolute() or ".." in storage_key.parts:
            raise ReportArtifactError("artifact storage key escapes its root")
        root = self.root.resolve()
        candidate = root.joinpath(*storage_key.parts).resolve()
        if not candidate.is_relative_to(root):
            raise ReportArtifactError("artifact storage key escapes its root")
        return candidate


def explicitly_requests_report_artifact(
    message: str,
    *,
    expected_version: int,
    artifact_format: str,
) -> bool:
    """Require one explicit DOCX render request bound to an exact Draft version."""

    text = " ".join(str(message or "").split())
    versions = {
        int(match.group(1))
        for pattern in _VERSION_PATTERNS
        for match in pattern.finditer(text)
    }
    return bool(
        expected_version > 0
        and artifact_format == REPORT_ARTIFACT_FORMAT_DOCX
        and text
        and _RENDER_ACTION_RE.search(text)
        and _DOCX_RE.search(text)
        and not _NEGATED_RE.search(text)
        and not _QUESTION_RE.search(text)
        and versions == {expected_version}
    )


def render_report_docx(*, markdown: str, template_bytes: bytes) -> bytes:
    """Render approved Markdown into a deterministic DOCX based on the trusted asset."""

    if not markdown.strip():
        raise ReportArtifactError("ReportDraft Markdown must not be empty")
    try:
        with zipfile.ZipFile(io.BytesIO(template_bytes), "r") as template:
            entries = {name: template.read(name) for name in template.namelist()}
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ReportArtifactError("trusted DOCX template is invalid") from exc
    if "word/styles.xml" not in entries or "word/document.xml" not in entries:
        raise ReportArtifactError("trusted DOCX template is incomplete")
    section_properties = _section_properties(entries["word/document.xml"])
    entries["word/document.xml"] = _document_xml(
        markdown,
        section_properties=section_properties,
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as rendered:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            rendered.writestr(info, entries[name])
    return output.getvalue()


def contract_digest(contract: WorkContractSnapshot) -> str:
    payload = {
        "contract_type": contract.contract_type,
        "contract_version": contract.contract_version,
        "status": contract.status.value,
        "payload": dict(contract.payload),
        "confirmed_by": contract.confirmed_by,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def artifact_id(
    *,
    tenant_id: str,
    work_id: str,
    content_sha256: str,
    source_digest: str,
    approval_digest: str,
    template_digest: str,
) -> str:
    material = "\0".join((
        tenant_id,
        work_id,
        content_sha256,
        source_digest,
        approval_digest,
        template_digest,
    )).encode()
    return f"artifact_{sha256(material).hexdigest()}"


def _section_properties(template_document_xml: bytes) -> str:
    try:
        document = template_document_xml.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportArtifactError("trusted DOCX document.xml is not UTF-8") from exc
    match = re.search(r"<w:sectPr(?:\s[^>]*)?>.*?</w:sectPr>", document, re.DOTALL)
    if match is None:
        raise ReportArtifactError("trusted DOCX template has no section properties")
    return match.group(0)


def _document_xml(markdown: str, *, section_properties: str) -> str:
    paragraphs = "".join(_markdown_paragraphs(markdown))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}" xmlns:r="{_R_NS}"><w:body>'
        f"{paragraphs}"
        f"{section_properties}</w:body></w:document>"
    )


def _markdown_paragraphs(markdown: str) -> tuple[str, ...]:
    paragraphs: list[str] = []
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line:
            paragraphs.append("<w:p/>")
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            style = "Title" if level == 1 else f"Heading{level - 1}"
            paragraphs.append(_paragraph(heading.group(2), style=style))
            continue
        blockquote = re.match(r"^\s*>+\s?(.*)$", line)
        if blockquote:
            paragraphs.append(_paragraph(blockquote.group(1)))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            text = re.sub(r"^\s*[-*+]\s+", "", line)
            paragraphs.append(_paragraph(f"• {text}"))
            continue
        ordered = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if ordered:
            paragraphs.append(_paragraph(f"{ordered.group(1)}. {ordered.group(2)}"))
            continue
        paragraphs.append(_paragraph(line.strip()))
    return tuple(paragraphs)


def _paragraph(text: str, *, style: str | None = None) -> str:
    clean = re.sub(r"(?<!\\)(?:\*\*|__|`)", "", text)
    style_xml = f'<w:pStyle w:val="{escape(style)}"/>' if style else ""
    return (
        f"<w:p><w:pPr>{style_xml}</w:pPr><w:r>"
        f'<w:t xml:space="preserve">{escape(clean)}</w:t></w:r></w:p>'
    )
