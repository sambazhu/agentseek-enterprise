from __future__ import annotations

from collections.abc import Mapping

from agentseek_files.models import FileRecord


def build_current_files_context(
    records: list[FileRecord],
    extracts: Mapping[str, str] | None = None,
    *,
    max_chars_per_file: int = 4_000,
) -> str:
    if not records:
        return ""
    extract_map = extracts or {}
    lines = ["[CurrentFiles]"]
    for record in records:
        lines.append(f"- file_id: {record.file_id}")
        lines.append(f"  filename: {record.sanitized_filename}")
        lines.append(f"  type: {record.mime_type}")
        lines.append(f"  size_bytes: {record.size_bytes}")
        lines.append(f"  extract_status: {record.extract_status}")
        text = extract_map.get(record.file_id, "")
        if text:
            excerpt = text[:max_chars_per_file]
            lines.append(f"  excerpt_chars: {len(excerpt)}")
            lines.append("  excerpt: |")
            for line in excerpt.splitlines() or [excerpt]:
                lines.append(f"    {line}")
    lines.append("[/CurrentFiles]")
    return "\n".join(lines)
