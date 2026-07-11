from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

_WHITESPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_CURRENCY_CHARS = "¥￥$€£"
_MAX_HEADERS = 30
_MAX_LAST_RECORD_CHARS = 500
_HEADER_SCAN_ROWS = 20
_HEADER_MARKERS = (
    "序号",
    "编号",
    "姓名",
    "名称",
    "班级",
    "年级",
    "部门",
    "日期",
    "时间",
    "金额",
    "数量",
    "明细",
    "name",
    "class",
    "date",
    "amount",
    "quantity",
)
_MARKDOWN_H1_RE = re.compile(r"(?m)^#(?!#)\s+(.+?)\s*$")
_SHEET_SELECTOR_RE = re.compile(
    r"(?:页签|sheet)\s*[A-Za-z0-9一二三四五六七八九十百]+|[一二三四五六七八九十百]+年级",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    sheet_name: str | None = None


@dataclass(frozen=True)
class SheetSummary:
    sheet_name: str
    table_count: int
    data_rows: int
    unique_people: int | None
    headers: tuple[str, ...]


@dataclass(frozen=True)
class SheetGroupCounts:
    sheet_name: str
    column: str
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ContentAnalysis:
    total_chars: int
    total_lines: int
    tables: tuple[ParsedTable, ...]

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def data_rows(self) -> int:
        return sum(len(table.rows) for table in self.tables)

    @property
    def headers(self) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for table in self.tables:
            for header in table.headers:
                if header and header not in seen:
                    seen.add(header)
                    result.append(header)
                    if len(result) >= _MAX_HEADERS:
                        return tuple(result)
        return tuple(result)

    @property
    def last_record(self) -> tuple[str, ...]:
        for table in reversed(self.tables):
            if table.rows:
                return table.rows[-1]
        return ()


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, bool]]]] = []
        self.table_sheet_names: list[str | None] = []
        self._table: list[list[tuple[str, bool]]] | None = None
        self._table_sheet_name: str | None = None
        self._row: list[tuple[str, bool]] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_is_header = False
        self._outside_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag == "table":
            self._table = []
            self._table_sheet_name = _nearest_h1("".join(self._outside_parts))
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._cell_is_header = tag == "th"
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        elif self._table is None:
            self._outside_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            self._row.append((_clean_cell("".join(self._cell_parts)), self._cell_is_header))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell, _ in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
                self.table_sheet_names.append(self._table_sheet_name)
            self._table = None
            self._table_sheet_name = None


def analyze_content(text: str, *, infer_sheet_names: bool = True) -> ContentAnalysis:
    parser = _TableParser()
    parser.feed(text)
    tables = tuple(
        _normalize_table(rows, sheet_name=sheet_name if infer_sheet_names else None)
        for rows, sheet_name in zip(parser.tables, parser.table_sheet_names, strict=True)
        if rows
    )
    return ContentAnalysis(
        total_chars=len(text),
        total_lines=text.count("\n") + (1 if text else 0),
        tables=tables,
    )


def format_large_file_summary(analysis: ContentAnalysis) -> list[str]:
    """Return bounded, model-facing facts about a complete extracted file."""
    lines = [
        f"完整解析字符数: {analysis.total_chars}",
        f"文本行数: {analysis.total_lines}",
        f"表格块数: {analysis.table_count}",
    ]
    if analysis.tables:
        lines.append(f"表格数据行数（不含表头）: {analysis.data_rows}")
    sheets = sheet_summaries(analysis)
    if sheets:
        lines.append("页签: " + "; ".join(f"{sheet.sheet_name}({sheet.data_rows} 行)" for sheet in sheets[:20]))
    if analysis.headers:
        lines.append(f"字段: {' | '.join(analysis.headers)}")
    if analysis.last_record:
        last_record = " | ".join(analysis.last_record)[:_MAX_LAST_RECORD_CHARS]
        lines.append(f"最后一条记录: {last_record}")
    ranges = numeric_ranges(analysis)
    if ranges:
        lines.append("数值范围: " + "; ".join(f"{name}={minimum}..{maximum}" for name, minimum, maximum in ranges))
    return lines


def numeric_ranges(analysis: ContentAnalysis) -> list[tuple[str, str, str]]:
    ranges: list[tuple[str, str, str]] = []
    for table_index, table in enumerate(analysis.tables, start=1):
        for column_index, header in enumerate(table.headers):
            values = [row[column_index] for row in table.rows if column_index < len(row) and row[column_index]]
            numbers = [number for value in values if (number := _parse_number(value)) is not None]
            if len(numbers) < 2 or len(numbers) / max(len(values), 1) < 0.6:
                continue
            label = header or f"表{table_index}第{column_index + 1}列"
            ranges.append((label, _format_decimal(min(numbers)), _format_decimal(max(numbers))))
            if len(ranges) >= 12:
                return ranges
    return ranges


def group_counts(analysis: ContentAnalysis, question: str) -> tuple[str, list[tuple[str, int]]] | None:
    """Infer a requested grouping column and count rows or unique people per value."""
    aggregated = _aggregate_group_counts(_tables_for_question(analysis, question), question)
    if aggregated is None:
        return None
    column, counts = aggregated
    return column, sorted(counts.items(), key=lambda item: item[0])


def group_counts_by_sheet(analysis: ContentAnalysis, question: str) -> list[SheetGroupCounts]:
    """Return the requested grouping independently for each named sheet."""
    grouped_tables: dict[str, list[ParsedTable]] = defaultdict(list)
    for table in _tables_for_question(analysis, question):
        if table.sheet_name:
            grouped_tables[table.sheet_name].append(table)

    result: list[SheetGroupCounts] = []
    for sheet_name, tables in grouped_tables.items():
        aggregated = _aggregate_group_counts(tables, question)
        if aggregated is None:
            continue
        column, counts = aggregated
        result.append(
            SheetGroupCounts(
                sheet_name=sheet_name,
                column=column,
                counts=tuple(sorted(counts.items(), key=lambda item: item[0])),
            )
        )
    return result


def sheet_summaries(analysis: ContentAnalysis) -> list[SheetSummary]:
    """Summarize tables associated with each MinerU-preserved Markdown H1."""
    grouped_tables: dict[str, list[ParsedTable]] = defaultdict(list)
    for table in analysis.tables:
        if table.sheet_name:
            grouped_tables[table.sheet_name].append(table)

    summaries: list[SheetSummary] = []
    for sheet_name, tables in grouped_tables.items():
        seen_headers: dict[str, None] = {}
        for table in tables:
            for header in table.headers:
                seen_headers.setdefault(header, None)
        summaries.append(
            SheetSummary(
                sheet_name=sheet_name,
                table_count=len(tables),
                data_rows=sum(len(table.rows) for table in tables),
                unique_people=_unique_people_in_tables(tables),
                headers=tuple(seen_headers)[:_MAX_HEADERS],
            )
        )
    return summaries


def _aggregate_group_counts(
    tables: list[ParsedTable],
    question: str,
) -> tuple[str, Counter[str]] | None:
    column: str | None = None
    row_counts: Counter[str] = Counter()
    names_by_group: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        group_index = _group_column_index(table.headers, question)
        if group_index is None:
            continue
        column = column or table.headers[group_index]
        name_index = _find_header(table.headers, ("姓名", "名字", "name"))
        if name_index is None:
            row_counts.update(row[group_index] for row in table.rows if group_index < len(row) and row[group_index])
        else:
            for row in table.rows:
                if group_index >= len(row) or not row[group_index]:
                    continue
                person = row[name_index] if name_index < len(row) else ""
                if person:
                    names_by_group[row[group_index]].add(person)
    if column is None:
        return None
    counts = Counter(row_counts)
    counts.update({group: len(people) for group, people in names_by_group.items()})
    return column, counts


def unique_people(analysis: ContentAnalysis) -> int | None:
    return _unique_people_in_tables(list(analysis.tables))


def _unique_people_in_tables(tables: list[ParsedTable]) -> int | None:
    people: set[str] = set()
    found_name_column = False
    for table in tables:
        name_index = _find_header(table.headers, ("姓名", "名字", "name"))
        if name_index is None:
            continue
        found_name_column = True
        people.update(row[name_index] for row in table.rows if name_index < len(row) and row[name_index])
    return len(people) if found_name_column else None


def matching_rows(analysis: ContentAnalysis, question: str, *, limit: int = 8) -> tuple[int, list[str]]:
    keywords = [word for word in re.findall(r"[\w\u4e00-\u9fff]+", question.lower()) if len(word) >= 2]
    if not keywords:
        return 0, []
    matches: list[str] = []
    total = 0
    for table in analysis.tables:
        for row in table.rows:
            rendered = " | ".join(row)
            if any(keyword in rendered.lower() for keyword in keywords):
                total += 1
                if len(matches) < limit:
                    matches.append(rendered[:500])
    return total, matches


def _normalize_table(
    raw_rows: list[list[tuple[str, bool]]],
    *,
    sheet_name: str | None,
) -> ParsedTable:
    header_index = _detect_header_index(raw_rows)
    header_row = raw_rows[header_index]
    headers = tuple(cell or f"列{index + 1}" for index, (cell, _) in enumerate(header_row))
    rows = tuple(tuple(cell for cell, _ in row) for row in raw_rows[header_index + 1 :])
    return ParsedTable(headers=headers, rows=rows, sheet_name=sheet_name)


def _nearest_h1(text: str) -> str | None:
    matches = list(_MARKDOWN_H1_RE.finditer(text))
    return matches[-1].group(1).strip() if matches else None


def _tables_for_question(analysis: ContentAnalysis, question: str) -> list[ParsedTable]:
    selectors = tuple(match.group(0).strip().lower() for match in _SHEET_SELECTOR_RE.finditer(question))
    if not selectors:
        return list(analysis.tables)
    selected = [
        table
        for table in analysis.tables
        if table.sheet_name and any(selector in table.sheet_name.lower() for selector in selectors)
    ]
    return selected or list(analysis.tables)


def _detect_header_index(raw_rows: list[list[tuple[str, bool]]]) -> int:
    """Find the real header after optional MinerU merged title rows."""
    scan_rows = raw_rows[:_HEADER_SCAN_ROWS]
    marker_candidates: list[tuple[int, int]] = []
    for index, row in enumerate(scan_rows):
        score = _header_marker_score(row)
        if score >= 2:
            marker_candidates.append((index, score))
    if marker_candidates:
        return max(marker_candidates, key=lambda candidate: (candidate[1], -candidate[0]))[0]

    widths = Counter(len(row) for row in raw_rows if row)
    if not widths:
        return 0
    modal_width = max(widths, key=lambda width: (widths[width], width))

    for index, row in enumerate(scan_rows):
        if len(row) == modal_width and any(is_header for _, is_header in row):
            return index
    for index, row in enumerate(raw_rows):
        if len(row) == modal_width:
            return index
    return 0


def _header_marker_score(row: list[tuple[str, bool]]) -> int:
    cells = {cell.strip().lower() for cell, _ in row if cell.strip()}
    return sum(any(marker in cell for marker in _HEADER_MARKERS) for cell in cells)


def _group_column_index(headers: tuple[str, ...], question: str) -> int | None:
    question_lower = question.lower()
    preferred_markers: tuple[str, ...] = ()
    if "班" in question:
        preferred_markers = ("班级", "班")
    elif "部门" in question:
        preferred_markers = ("部门",)
    elif "年级" in question:
        preferred_markers = ("年级",)
    elif "日期" in question or "每天" in question or "每日" in question:
        preferred_markers = ("日期", "时间")
    for markers in (preferred_markers, tuple(header for header in headers if header.lower() in question_lower)):
        index = _find_header(headers, markers)
        if index is not None:
            return index
    return None


def _find_header(headers: tuple[str, ...], markers: tuple[str, ...]) -> int | None:
    lowered = tuple(marker.lower() for marker in markers)
    for index, header in enumerate(headers):
        if any(marker and marker in header.lower() for marker in lowered):
            return index
    return None


def _parse_number(value: str) -> Decimal | None:
    normalized = value.strip().strip(_CURRENCY_CHARS).replace(",", "").replace("，", "")
    percentage = normalized.endswith("%")
    if percentage:
        normalized = normalized[:-1]
    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1]
    if not _NUMBER_RE.fullmatch(normalized):
        return None
    try:
        number = Decimal(normalized)
    except InvalidOperation:
        return None
    if negative:
        number = -number
    return number


def _format_decimal(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def _clean_cell(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()
