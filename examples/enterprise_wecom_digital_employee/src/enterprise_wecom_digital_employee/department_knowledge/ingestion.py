from __future__ import annotations

import re
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from enterprise_wecom_digital_employee.department_knowledge.models import KnowledgeChunk, KnowledgeDocument

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAX_IMPORT_BYTES = 50 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 64 * 1024 * 1024
_HEADING_RE = re.compile(r"^(?:Heading|标题)\s*([1-6])$", re.IGNORECASE)


def load_document(
    path: Path,
    *,
    namespace: str,
    title: str | None = None,
    confidentiality_level: str = "internal",
) -> KnowledgeDocument:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError("knowledge source must be a regular non-symlink file")
    source = candidate.resolve(strict=True)
    if not source.is_file():
        raise ValueError("knowledge source must be a regular non-symlink file")
    size = source.stat().st_size
    if size <= 0 or size > _MAX_IMPORT_BYTES:
        raise ValueError("knowledge source size is outside the accepted range")
    content = source.read_bytes()
    suffix = source.suffix.lower()
    if suffix in {".md", ".txt"}:
        text = content.decode("utf-8")
    elif suffix == ".docx":
        text = extract_docx_text(content)
    else:
        raise ValueError("local department knowledge import supports .docx, .md, and .txt")
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("knowledge source did not contain extractable text")
    source_name = source.name
    document_id = f"dk_{sha256(f'{namespace}\0{source_name}'.encode()).hexdigest()}"
    return KnowledgeDocument(
        document_id=document_id,
        title=(title or _infer_title(clean_text, source.stem)).strip(),
        source_name=source_name,
        source_sha256=sha256(content).hexdigest(),
        text=clean_text,
        confidentiality_level=confidentiality_level,
        metadata={"source_suffix": suffix, "source_size": size},
    )


def extract_docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > _MAX_DOCX_XML_BYTES:
                raise ValueError("DOCX document.xml exceeds the accepted size")
            # OOXML is size-bounded above and Word document.xml does not require DTD/entity expansion.
            root = ElementTree.fromstring(archive.read(info))  # noqa: S314
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ValueError("invalid DOCX knowledge source") from exc

    body = root.find(f"{_WORD_NS}body")
    if body is None:
        return ""
    blocks: list[str] = []
    for element in body:
        if element.tag == f"{_WORD_NS}p":
            text = _paragraph_text(element)
            if not text:
                continue
            heading_level = _heading_level(element)
            blocks.append(f"{'#' * heading_level} {text}" if heading_level else text)
        elif element.tag == f"{_WORD_NS}tbl":
            rows = _table_rows(element)
            if rows:
                blocks.extend(rows)
    return "\n\n".join(blocks)


def chunk_document(
    document: KnowledgeDocument,
    *,
    max_chars: int,
    overlap_chars: int,
) -> tuple[KnowledgeChunk, ...]:
    if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("invalid knowledge chunk size")
    blocks = [block.strip() for block in re.split(r"\n\s*\n", document.text) if block.strip()]
    chunks: list[KnowledgeChunk] = []
    current: list[str] = []
    current_chars = 0
    heading = ""
    chunk_heading = ""

    def flush() -> None:
        nonlocal current, current_chars, chunk_heading
        content = "\n\n".join(current).strip()
        if not content:
            return
        ordinal = len(chunks)
        chunk_id = f"{document.document_id}:chunk:{ordinal:05d}"
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                ordinal=ordinal,
                heading=chunk_heading,
                content=content,
            )
        )
        overlap = content[-overlap_chars:].lstrip() if overlap_chars else ""
        current = [overlap] if overlap else []
        current_chars = len(overlap)
        chunk_heading = heading

    for block in blocks:
        if block.startswith("#"):
            flush()
            heading = block.lstrip("#").strip()
            chunk_heading = heading
        for part in _split_long_block(block, max_chars):
            separator = 2 if current else 0
            if current and current_chars + separator + len(part) > max_chars:
                flush()
                if current_chars + (2 if current else 0) + len(part) > max_chars:
                    current = []
                    current_chars = 0
            if not current:
                chunk_heading = heading
            current.append(part)
            current_chars += (2 if len(current) > 1 else 0) + len(part)
    flush()
    return tuple(chunks)


def _paragraph_text(element: ElementTree.Element) -> str:
    values: list[str] = []
    for node in element.iter():
        if node.tag == f"{_WORD_NS}t" and node.text:
            values.append(node.text)
        elif node.tag in {f"{_WORD_NS}tab", f"{_WORD_NS}br"}:
            values.append("\t" if node.tag.endswith("tab") else "\n")
    return "".join(values).strip()


def _heading_level(element: ElementTree.Element) -> int:
    style = element.find(f"{_WORD_NS}pPr/{_WORD_NS}pStyle")
    raw = style.get(f"{_WORD_NS}val", "") if style is not None else ""
    match = _HEADING_RE.fullmatch(raw)
    return int(match.group(1)) if match else 0


def _table_rows(element: ElementTree.Element) -> list[str]:
    rows: list[str] = []
    for row in element.findall(f"{_WORD_NS}tr"):
        cells = [
            " ".join(filter(None, (_paragraph_text(paragraph) for paragraph in cell.findall(f"{_WORD_NS}p"))))
            for cell in row.findall(f"{_WORD_NS}tc")
        ]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _split_long_block(block: str, max_chars: int) -> tuple[str, ...]:
    if len(block) <= max_chars:
        return (block,)
    return tuple(block[start : start + max_chars] for start in range(0, len(block), max_chars))


def _infer_title(text: str, fallback: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    title = first.lstrip("#").strip()
    return title[:200] or fallback
