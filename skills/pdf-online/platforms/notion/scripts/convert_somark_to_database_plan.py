#!/usr/bin/env python3
"""Build a deterministic Notion database plan from SoMark table JSON."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PLAN_FORMAT = "somark-to-notion-database-plan-v1"
TITLE_CANDIDATES = ("任务名称", "名称", "标题", "项目", "事项")
START_DATE_NAMES = ("开始日期", "起始日期")
END_DATE_NAMES = ("结束日期", "截止日期")
SELECT_NAMES = ("状态", "类别", "分类", "类型", "优先级")
PERSON_SIGNALS = ("负责人", "人员", "成员", "姓名")
RELATION_SIGNALS = ("前置任务", "依赖", "关联")
UNSUPPORTED_SIGNALS = ("附件", "网址", "链接", "URL", "多选", "公式", "汇总")
IDENTIFIER_SIGNALS = (
    "id", "编号", "序号", "代码", "学号", "工号", "订单号", "证件",
    "电话", "手机", "邮编", "账号", "条码", "批次",
)
INTERNAL_ROW_PROPERTY_BASE = "SoMark 源行序"
NUMBER_PATTERN = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)\Z")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class TableSelectionRequired(ValueError):
    """Signal that a human must choose one of several source tables."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = candidates
        super().__init__(
            "multiple tables found; choose one by supplying both --table-page and --table-idx: "
            + json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
        )


@dataclass(frozen=True)
class SourceCell:
    content: str
    tag: str
    rowspan: int
    colspan: int


@dataclass(frozen=True)
class CellAnchor:
    row: int
    column: int
    cell: SourceCell


@dataclass(frozen=True)
class ParsedTable:
    rows: list[list[str]]
    anchors: list[CellAnchor]
    merges: list[dict[str, Any]]
    issues: list[str]


class TableParser(HTMLParser):
    """Parse SoMark HTML tables while retaining rowspan and colspan geometry."""

    INLINE_TAGS = {"span", "strong", "b", "em", "i", "u", "s", "a", "code", "sup", "sub"}
    LINE_TAGS = {"br", "p", "div"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[SourceCell]] = []
        self.issues: list[str] = []
        self._row: list[SourceCell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_tag: str | None = None
        self._rowspan = 1
        self._colspan = 1
        self._table_depth = 0

    @staticmethod
    def _span(value: str | None, name: str) -> int:
        if value is None:
            return 1
        try:
            result = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if result < 1:
            raise ValueError(f"{name} must be a positive integer")
        return result

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth > 1:
                self.issues.append("nested_table_content_flattened")
            return
        if self._table_depth > 1:
            if tag in self.LINE_TAGS and self._cell_parts is not None:
                self._cell_parts.append("\n")
            return
        if tag == "tr":
            if self._row is not None:
                self.issues.append("nested_or_malformed_row_ignored")
                return
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None or self._cell_parts is not None:
                raise ValueError("table cells must belong to one non-nested row")
            attr_map = {name.lower(): value for name, value in attrs}
            self._rowspan = self._span(attr_map.get("rowspan"), "rowspan")
            self._colspan = self._span(attr_map.get("colspan"), "colspan")
            self._cell_tag = tag
            self._cell_parts = []
        elif tag in self.LINE_TAGS:
            if self._cell_parts is not None and self._cell_parts and not self._cell_parts[-1].endswith("\n"):
                self._cell_parts.append("\n")
        elif tag in self.INLINE_TAGS or tag in {"tbody", "thead", "tfoot"}:
            return
        elif tag == "img" and self._cell_parts is not None:
            attr_map = {name.lower(): value for name, value in attrs}
            source = attr_map.get("src") or ""
            if source:
                self._cell_parts.append(source)
        else:
            self.issues.append(f"unsupported_tag_ignored:{tag}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._table_depth > 1:
            if tag == "table":
                self._table_depth -= 1
            elif tag in self.LINE_TAGS and self._cell_parts is not None:
                self._cell_parts.append("\n")
            return
        if tag in {"td", "th"}:
            if self._row is None or self._cell_parts is None:
                raise ValueError(f"unexpected </{tag}>")
            self._row.append(
                SourceCell(
                    content="".join(self._cell_parts),
                    tag=self._cell_tag or tag,
                    rowspan=self._rowspan,
                    colspan=self._colspan,
                )
            )
            self._cell_parts = None
            self._cell_tag = None
        elif tag == "tr":
            if self._row is None or self._cell_parts is not None:
                raise ValueError("invalid table row termination")
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._table_depth -= 1
        elif tag in self.LINE_TAGS:
            if self._cell_parts is not None and self._cell_parts and not self._cell_parts[-1].endswith("\n"):
                self._cell_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        elif data.strip():
            self.issues.append("text_outside_cells_ignored")

    def finish(self) -> list[list[SourceCell]]:
        self.close()
        if self._row is not None or self._cell_parts is not None or self._table_depth != 0:
            raise ValueError("incomplete HTML table")
        if not self.rows:
            raise ValueError("empty HTML table")
        return self.rows


def parse_table(html: str) -> ParsedTable:
    parser = TableParser()
    parser.feed(html)
    source_rows = parser.finish()
    occupied: set[tuple[int, int]] = set()
    values: dict[tuple[int, int], str] = {}
    anchors: list[CellAnchor] = []
    merges: list[dict[str, Any]] = []
    max_column = 0

    issues = list(parser.issues)
    for row_index, source_row in enumerate(source_rows, start=1):
        column_index = 1
        for cell in source_row:
            while (row_index, column_index) in occupied:
                column_index += 1
            effective_rowspan = min(cell.rowspan, len(source_rows) - row_index + 1)
            if effective_rowspan != cell.rowspan:
                issues.append(f"rowspan_clipped_at_row:{row_index}")
            while any(
                (target_row, target_column) in occupied
                for target_row in range(row_index, row_index + effective_rowspan)
                for target_column in range(column_index, column_index + cell.colspan)
            ):
                column_index += 1
                issues.append(f"overlapping_span_shifted_at_row:{row_index}")
            for target_row in range(row_index, row_index + effective_rowspan):
                for target_column in range(column_index, column_index + cell.colspan):
                    coordinate = (target_row, target_column)
                    occupied.add(coordinate)
                    values[coordinate] = ""
            values[(row_index, column_index)] = cell.content
            effective_cell = SourceCell(
                content=cell.content,
                tag=cell.tag,
                rowspan=effective_rowspan,
                colspan=cell.colspan,
            )
            anchors.append(CellAnchor(row_index, column_index, effective_cell))
            if effective_rowspan > 1 or cell.colspan > 1:
                merges.append(
                    {
                        "start_row": row_index,
                        "start_column": column_index,
                        "rowspan": effective_rowspan,
                        "colspan": cell.colspan,
                        "original_value": cell.content,
                    }
                )
            max_column = max(max_column, column_index + cell.colspan - 1)
            column_index += cell.colspan

    for row_index in range(1, len(source_rows) + 1):
        missing = [column for column in range(1, max_column + 1) if (row_index, column) not in occupied]
        if missing:
            issues.append(f"non_rectangular_row_padded:{row_index}")
            for column in missing:
                occupied.add((row_index, column))
                values[(row_index, column)] = ""
    rows = [
        [values[(row, column)] for column in range(1, max_column + 1)]
        for row in range(1, len(source_rows) + 1)
    ]
    return ParsedTable(rows=rows, anchors=anchors, merges=merges, issues=unique_nonempty(issues))


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def extract_pages(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, str]:
    """Accept extracted pages or the supported official SoMark response wrappers."""
    paths = (
        ("pages",),
        ("data", "result", "outputs", "json", "pages"),
        ("result", "outputs", "json", "pages"),
        ("outputs", "json", "pages"),
    )
    pages = None
    payload_shape = ""
    for path in paths:
        candidate = _nested(payload, *path)
        if candidate is not None:
            pages = candidate
            payload_shape = "extracted_pages" if path == ("pages",) else "official_api_response"
            break
    if not isinstance(pages, list):
        raise ValueError("input must be an official SoMark response or an object containing pages")
    for position, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            raise ValueError(f"page at position {position} must contain a blocks array")
    source_file = (
        _nested(payload, "data", "result", "file_name")
        or _nested(payload, "data", "file_name")
        or _nested(payload, "result", "file_name")
        or payload.get("file_name")
    )
    return pages, source_file if isinstance(source_file, str) else None, payload_shape


def collect_tables(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for page_position, page in enumerate(pages):
        page_num = page.get("page_num", page_position)
        for block_position, block in enumerate(page["blocks"]):
            if not isinstance(block, dict) or block.get("type") != "table":
                continue
            idx = block.get("idx", block_position)
            if not isinstance(page_num, int) or not isinstance(idx, int):
                raise ValueError("table page_num and idx must be integers")
            content = block.get("content")
            if not isinstance(content, str):
                raise ValueError(f"table at page {page_num}, idx {idx} has no HTML content")
            tables.append({"page_num": page_num, "idx": idx, "content": content})
    tables.sort(key=lambda item: (item["page_num"], item["idx"]))
    identities = [(item["page_num"], item["idx"]) for item in tables]
    if len(identities) != len(set(identities)):
        raise ValueError("table (page_num, idx) identities must be unique")
    return tables


def table_candidate(table: dict[str, Any]) -> dict[str, Any]:
    candidate: dict[str, Any] = {"page_num": table["page_num"], "idx": table["idx"]}
    try:
        parsed = parse_table(table["content"])
        candidate.update(
            raw_header=parsed.rows[0] if parsed.rows else [],
            data_row_count=max(0, len(parsed.rows) - 1),
        )
    except ValueError as exc:
        candidate["validation_error"] = str(exc)
    return candidate


def choose_table(
    tables: list[dict[str, Any]], *, table_page_num: int | None, table_idx: int | None
) -> tuple[dict[str, Any], str]:
    if not tables:
        raise ValueError("no table blocks found; database mode requires a business-record table")
    if (table_page_num is None) != (table_idx is None):
        raise ValueError("table selection requires both page_num and idx")
    if table_page_num is not None:
        matches = [
            table for table in tables
            if table["page_num"] == table_page_num and table["idx"] == table_idx
        ]
        if not matches:
            raise ValueError(f"no table found at page {table_page_num}, idx {table_idx}")
        return matches[0], "explicit_page_and_idx"
    if len(tables) > 1:
        raise TableSelectionRequired([table_candidate(table) for table in tables])
    return tables[0], "only_table"


def unique_property_name(raw_name: str, physical_index: int, used: set[str]) -> tuple[str, str | None]:
    base = raw_name.strip()
    reason = None
    if not base:
        base = f"列{physical_index}"
        reason = "empty_header_generated"
    name = base
    suffix = 2
    while name in used:
        name = f"{base} ({suffix})"
        suffix += 1
        reason = "duplicate_header_renamed"
    used.add(name)
    return name, reason


def header_row_count(parsed: ParsedTable) -> int:
    """Infer a small multi-row header without making complex layout a hard failure."""
    if len(parsed.rows) <= 1:
        return 1
    first_row_anchors = [anchor for anchor in parsed.anchors if anchor.row == 1]
    rowspan_depth = max((anchor.cell.rowspan for anchor in first_row_anchors), default=1)
    if rowspan_depth > 1:
        return min(rowspan_depth, len(parsed.rows) - 1)
    return 1


def logical_columns(parsed: ParsedTable) -> tuple[list[dict[str, Any]], int]:
    if not parsed.rows or not parsed.rows[0]:
        raise ValueError("table header is empty")
    header_rows = header_row_count(parsed)
    physical_width = len(parsed.rows[0])
    used: set[str] = set()
    if header_rows == 1:
        columns: list[dict[str, Any]] = []
        header_anchors = [anchor for anchor in parsed.anchors if anchor.row == 1]
        for logical_index, anchor in enumerate(header_anchors, start=1):
            name, normalization_reason = unique_property_name(
                anchor.cell.content, logical_index, used
            )
            columns.append(
                {
                    "name": name,
                    "raw_name": anchor.cell.content,
                    "logical_index": logical_index,
                    "source_columns": list(
                        range(anchor.column, anchor.column + anchor.cell.colspan)
                    ),
                    "header_colspan": anchor.cell.colspan,
                    "normalization_reason": normalization_reason,
                }
            )
        if columns:
            return columns, header_rows

    expanded_headers = [["" for _ in range(physical_width)] for _ in range(header_rows)]
    for anchor in parsed.anchors:
        if anchor.row > header_rows:
            continue
        for row in range(anchor.row, min(header_rows, anchor.row + anchor.cell.rowspan - 1) + 1):
            for column in range(anchor.column, anchor.column + anchor.cell.colspan):
                if column <= physical_width:
                    expanded_headers[row - 1][column - 1] = anchor.cell.content.strip()
    columns = []
    for physical_index in range(1, physical_width + 1):
        parts: list[str] = []
        for row in expanded_headers:
            value = row[physical_index - 1]
            if value and value not in parts:
                parts.append(value)
        raw_name = " / ".join(parts)
        name, normalization_reason = unique_property_name(raw_name, physical_index, used)
        columns.append(
            {
                "name": name,
                "raw_name": raw_name,
                "logical_index": physical_index,
                "source_columns": [physical_index],
                "header_colspan": 1,
                "normalization_reason": normalization_reason,
            }
        )
    return columns, header_rows


def _anchor_within_one_column(anchor: CellAnchor, columns: list[dict[str, Any]]) -> bool:
    anchor_columns = set(range(anchor.column, anchor.column + anchor.cell.colspan))
    return any(anchor_columns.issubset(set(column["source_columns"])) for column in columns)


def normalize_data_rows(
    parsed: ParsedTable, columns: list[dict[str, Any]], header_rows: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(parsed.rows[header_rows:], start=header_rows + 1):
        values = {
            column["name"]: "".join(
                row[physical_index - 1] for physical_index in column["source_columns"]
            )
            for column in columns
        }
        records.append(
            {
                "source_row_number": row_number,
                "raw_expanded_cells": list(row),
                "original_values": values,
            }
        )
    return records, []


def is_iso_date(value: str) -> bool:
    if not ISO_DATE_PATTERN.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_number(value: str) -> bool:
    return bool(NUMBER_PATTERN.fullmatch(value))


def number_value(value: str) -> int | float | None:
    if value == "":
        return None
    if "." not in value:
        return int(value)
    return float(value)


def unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def low_cardinality(values: list[str]) -> bool:
    nonempty = [value for value in values if value]
    if not nonempty:
        return False
    limit = max(2, min(20, (len(nonempty) + 1) // 2))
    return len(set(nonempty)) <= limit


def internal_row_property_name(source_names: list[str], property_names: list[str]) -> str:
    used = set(source_names) | set(property_names)
    if INTERNAL_ROW_PROPERTY_BASE not in used:
        return INTERNAL_ROW_PROPERTY_BASE
    suffix = 2
    while f"{INTERNAL_ROW_PROPERTY_BASE} ({suffix})" in used:
        suffix += 1
    return f"{INTERNAL_ROW_PROPERTY_BASE} ({suffix})"


def collision_safe_property_name(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    suffix = 2
    while f"{base} ({suffix})" in used:
        suffix += 1
    return f"{base} ({suffix})"


def should_preserve_numeric_text(name: str, values: list[str]) -> bool:
    lowered_name = name.casefold()
    if any(signal.casefold() in lowered_name for signal in IDENTIFIER_SIGNALS):
        return True
    return any(re.fullmatch(r"[+-]?0\d+", value) for value in values if value)


def build_properties_and_records(
    columns: list[dict[str, Any]], source_records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    names = [column["name"] for column in columns]
    warnings: list[str] = []
    degradations: list[dict[str, Any]] = []
    title_index = 0
    for candidate in TITLE_CANDIDATES:
        if candidate in names:
            title_index = names.index(candidate)
            break
    title_name = names[title_index]
    if any(
        not record["original_values"][title_name].strip()
        and any(value.strip() for value in record["original_values"].values())
        for record in source_records
    ):
        warnings.append(f"标题属性 {title_name} 存在空值；对应记录可能在 Notion 中显示为无标题。")

    start_indices = [index for index, name in enumerate(names) if name in START_DATE_NAMES]
    end_indices = [index for index, name in enumerate(names) if name in END_DATE_NAMES]
    range_pair: tuple[int, int] | None = None
    if len(start_indices) == 1 and len(end_indices) == 1:
        candidate_pair = (start_indices[0], end_indices[0])
        start_name, end_name = names[candidate_pair[0]], names[candidate_pair[1]]
        range_is_safe = all(
            (not start or is_iso_date(start))
            and (not end or is_iso_date(end))
            and not (end and not start)
            and not (start and end and end < start)
            for record in source_records
            for start, end in [
                (
                    record["original_values"][start_name],
                    record["original_values"][end_name],
                )
            ]
        )
        if range_is_safe:
            range_pair = candidate_pair
        else:
            warnings.append("开始/结束日期不能无损合并为日期范围，已分别降级为文本列以保留原值。")
            for date_name in (start_name, end_name):
                degradations.append(
                    {
                        "property": date_name,
                        "from": "date_range_candidate",
                        "to": "rich_text",
                        "reason": "unsafe_or_partial_date_range",
                    }
                )
    elif start_indices or end_indices:
        warnings.append("开始/结束日期列不成唯一配对，已逐列保留并仅在值完全可靠时使用日期类型。")

    properties: list[dict[str, Any]] = []
    conversion_specs: list[dict[str, Any]] = []
    skip_indices = {range_pair[1]} if range_pair else set()
    range_property_name = (
        collision_safe_property_name("日期范围", set(names)) if range_pair else None
    )

    for index, name in enumerate(names):
        if index in skip_indices:
            continue
        source_values = [record["original_values"][name] for record in source_records]
        if index == title_index:
            property_spec = {"source_columns": [name], "name": name, "type": "title"}
            conversion = {"kind": "title", "source_columns": [name], "name": name}
        elif range_pair and index == range_pair[0]:
            start_name, end_name = names[range_pair[0]], names[range_pair[1]]
            assert range_property_name is not None
            property_spec = {
                "source_columns": [start_name, end_name],
                "name": range_property_name,
                "type": "date",
                "range": True,
            }
            conversion = {
                "kind": "date_range",
                "source_columns": [start_name, end_name],
                "name": range_property_name,
            }
            warnings.append(
                f"开始日期和结束日期合并为 {range_property_name}；original_values 保留两个原值。"
            )
        else:
            nonempty_values = [value for value in source_values if value]
            explicit_date = "日期" in name
            semantic_rich_text = (
                any(signal in name for signal in PERSON_SIGNALS)
                or any(signal in name for signal in RELATION_SIGNALS)
                or any(signal in name for signal in UNSUPPORTED_SIGNALS)
            )
            if semantic_rich_text:
                property_type = "rich_text"
            elif (
                nonempty_values
                and all(is_number(value) for value in nonempty_values)
                and not should_preserve_numeric_text(name, nonempty_values)
            ):
                property_type = "number"
            elif nonempty_values and all(is_iso_date(value) for value in nonempty_values):
                property_type = "date"
            elif name in SELECT_NAMES and low_cardinality(source_values):
                property_type = "select"
            else:
                property_type = "rich_text"
            property_spec = {"source_columns": [name], "name": name, "type": property_type}
            if explicit_date and nonempty_values and property_type == "rich_text":
                degradations.append(
                    {
                        "property": name,
                        "from": "date-like text",
                        "to": "rich_text",
                        "reason": "mixed_or_invalid_date_values",
                    }
                )
                warnings.append(f"{name} 含非标准或混合日期值，已按文本保留。")
            if property_type == "select":
                property_spec["options"] = unique_nonempty(source_values)
                warnings.append(f"{name} 使用 select 而非原生 status，以完整保留源选项。")
            conversion = {"kind": property_type, "source_columns": [name], "name": name}

        properties.append(property_spec)
        conversion_specs.append(conversion)
        if property_spec["type"] == "rich_text":
            if any(signal in name for signal in PERSON_SIGNALS):
                degradations.append(
                    {"property": name, "from": "person-like text", "to": "rich_text", "reason": "people_not_supported"}
                )
            elif any(signal in name for signal in RELATION_SIGNALS):
                degradations.append(
                    {"property": name, "from": "relation-like text", "to": "rich_text", "reason": "relation_not_supported"}
                )
            elif any(signal in name for signal in UNSUPPORTED_SIGNALS):
                degradations.append(
                    {"property": name, "from": "unsupported_semantic_type", "to": "rich_text", "reason": "mvp_type_fallback"}
                )

    property_names = [prop["name"] for prop in properties]
    if len(property_names) != len(set(property_names)):
        raise ValueError("normalized Notion property names are not unique: " + repr(property_names))
    row_property = internal_row_property_name(names, property_names)
    properties.append(
        {
            "source_columns": [],
            "name": row_property,
            "type": "number",
            "internal": True,
        }
    )

    records: list[dict[str, Any]] = []
    for source_record in source_records:
        original = dict(source_record["original_values"])
        converted: dict[str, Any] = {}
        for spec in conversion_specs:
            name = spec["name"]
            values = [original[column] for column in spec["source_columns"]]
            if spec["kind"] == "number":
                converted[name] = number_value(values[0])
            elif spec["kind"] == "date":
                converted[name] = {"start": values[0], "end": None} if values[0] else None
            elif spec["kind"] == "date_range":
                converted[name] = {"start": values[0], "end": values[1] or None} if values[0] else None
            elif spec["kind"] == "select":
                converted[name] = values[0] or None
            else:
                converted[name] = values[0]
        converted[row_property] = source_record["source_row_number"]
        records.append(
            {
                "source_row_number": source_record["source_row_number"],
                "raw_expanded_cells": source_record["raw_expanded_cells"],
                "original_values": original,
                "converted_values": converted,
            }
        )
    return properties, records, warnings, degradations


def assess_database_suitability(
    parsed: ParsedTable,
    columns: list[dict[str, Any]],
    records: list[dict[str, Any]],
    properties: list[dict[str, Any]],
    header_rows: int,
) -> dict[str, Any]:
    risks: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        if not any(item["code"] == code for item in risks):
            risks.append({"code": code, "message": message})

    if parsed.issues:
        add("html_repaired", "表格 HTML 存在不规则结构，转换器已尽力修复或补齐。")
    if header_rows > 1:
        add("multi_row_header", "多行表头已扁平化为数据库字段名，字段含义可能需要人工核对。")
    if header_rows > 1 and any(
        anchor.row <= header_rows and (anchor.cell.rowspan > 1 or anchor.cell.colspan > 1)
        for anchor in parsed.anchors
    ):
        add("merged_header", "合并表头已展开为独立数据库列。")
    body_merges = [
        anchor
        for anchor in parsed.anchors
        if anchor.row > header_rows
        and (
            anchor.cell.rowspan > 1
            or (
                anchor.cell.colspan > 1
                and not _anchor_within_one_column(anchor, columns)
            )
        )
    ]
    if body_merges:
        add("merged_body_cells", "数据区合并单元格已展开；只有原锚点保留内容，其余格为空。")
    physical_width = len(parsed.rows[0]) if parsed.rows else 0
    if any(
        anchor.row > header_rows
        and anchor.column == 1
        and anchor.cell.colspan >= physical_width
        and physical_width > 1
        for anchor in parsed.anchors
    ):
        add("section_rows", "表内分组标题行会作为普通数据库记录保留，列关系可能不直观。")
    if any(column.get("normalization_reason") for column in columns):
        add("normalized_headers", "空白或重复表头已生成唯一字段名。")
    if not records:
        add("no_data_records", "源表没有非空数据行，将只创建数据库结构。")
    title_name = next(prop["name"] for prop in properties if prop["type"] == "title")
    if any(
        not str(record["converted_values"].get(title_name, "")).strip()
        and any(value.strip() for value in record["original_values"].values())
        for record in records
    ):
        add("empty_title_values", "部分记录的标题字段为空，Notion 中可能显示为无标题。")

    requires_confirmation = bool(risks)
    return {
        "recommended": not requires_confirmation,
        "requires_confirmation": requires_confirmation,
        "can_force_continue": True,
        "decision": "ask_before_write" if requires_confirmation else "proceed",
        "message": (
            "该表结构不完全适合数据库，继续后会按尽力转换和文本降级策略写入。是否仍要生成数据库？"
            if requires_confirmation
            else "该表可直接转换为数据库。"
        ),
        "risks": risks,
        "fallback_strategy": "preserve_rows_and_columns_then_degrade_uncertain_properties_to_rich_text",
    }


def validate_plan(plan: dict[str, Any]) -> dict[str, bool]:
    properties = plan["properties"]
    records = plan["records"]
    names = [prop["name"] for prop in properties]
    title_properties = [prop for prop in properties if prop["type"] == "title"]
    title_name = title_properties[0]["name"] if len(title_properties) == 1 else None
    date_names = [prop["name"] for prop in properties if prop["type"] == "date"]
    row_property = plan["stable_sort_rule"]["property"]
    checks = {
        "format_supported": plan.get("format") == PLAN_FORMAT,
        "unique_property_names": len(names) == len(set(names)),
        "exactly_one_title": len(title_properties) == 1,
        "record_count_nonnegative": len(records) >= 0,
        "all_titles_are_strings": bool(title_name) and all(
            isinstance(record["converted_values"].get(title_name), str)
            for record in records
        ),
        "all_dates_valid": all(
            value is None
            or (
                isinstance(value, dict)
                and is_iso_date(value.get("start", ""))
                and (value.get("end") is None or is_iso_date(value["end"]))
            )
            for record in records
            for name in date_names
            for value in [record["converted_values"].get(name)]
        ),
        "all_source_rows_accounted_for": (
            len(records) + len(plan["filtered_rows"]) == plan["source_table"]["data_row_count"]
        ),
        "source_values_preserved": all(
            list(record["original_values"]) == plan["source_table"]["normalized_logical_column_names"]
            for record in records
        ),
        "view_order_matches_properties": (
            plan["view_column_order"] == [prop["name"] for prop in properties if not prop.get("internal")]
        ),
        "source_row_property_present": row_property in names,
        "source_row_sort_values_unique": len(
            {record["converted_values"].get(row_property) for record in records}
        ) == len(records),
        "source_row_property_hidden": row_property in plan["default_table_view"]["hide"],
        "source_row_sort_ascending": (
            plan["default_table_view"]["sort_by"]
            == {"property": row_property, "direction": "ascending"}
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ValueError("prewrite validation failed: " + ", ".join(failed))
    return checks


def build_plan(
    source_path: Path,
    *,
    database_name: str | None = None,
    source_file: str | None = None,
    table_page_num: int | None = None,
    table_idx: int | None = None,
) -> dict[str, Any]:
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("input JSON root must be an object")
    pages, detected_source_file, payload_shape = extract_pages(payload)
    tables = collect_tables(pages)
    selected, selection_rule = choose_table(
        tables, table_page_num=table_page_num, table_idx=table_idx
    )
    parsed = parse_table(selected["content"])
    columns, header_rows = logical_columns(parsed)
    source_records, filtered_rows = normalize_data_rows(parsed, columns, header_rows)
    properties, records, warnings, degradations = build_properties_and_records(
        columns, source_records
    )
    suitability = assess_database_suitability(
        parsed, columns, records, properties, header_rows
    )
    if suitability["requires_confirmation"]:
        warnings.extend(item["message"] for item in suitability["risks"])
    logical_source_file = source_file or detected_source_file or source_path.name
    logical_database_name = database_name or Path(logical_source_file).stem
    if not logical_database_name.strip():
        raise ValueError("database name must not be empty")
    row_property = next(prop["name"] for prop in properties if prop.get("internal"))
    view_columns = [prop["name"] for prop in properties if not prop.get("internal")]
    show_dsl = "SHOW " + ", ".join(json.dumps(name, ensure_ascii=False) for name in view_columns)
    sort_dsl = f'SORT BY "{row_property}" ASC'
    hide_dsl = f'HIDE "{row_property}"'
    freeze_dsl = "FREEZE COLUMNS 1"
    wrap_dsl = "WRAP CELLS true"
    view = {
        "type": "table",
        "show": view_columns,
        "show_dsl": show_dsl,
        "sort_by": {"property": row_property, "direction": "ascending"},
        "sort_dsl": sort_dsl,
        "hide": [row_property],
        "hide_dsl": hide_dsl,
        "freeze_first_column": True,
        "wrap_cells": True,
        "freeze_dsl": freeze_dsl,
        "wrap_dsl": wrap_dsl,
        "configure_dsl": "; ".join(
            [show_dsl, sort_dsl, hide_dsl, freeze_dsl, wrap_dsl]
        ),
    }
    if parsed.merges:
        warnings.append("HTML rowspan/colspan 已按物理网格解析；跨行单元格仅在左上角保留源值。")
    plan: dict[str, Any] = {
        "format": PLAN_FORMAT,
        "database_name": logical_database_name,
        "source_file": logical_source_file,
        "source_json": source_path.as_posix(),
        "input_shape": payload_shape,
        "source_table": {
            "page_num": selected["page_num"],
            "idx": selected["idx"],
            "table_count": len(tables),
            "selection_rule": selection_rule,
            "raw_header": parsed.rows[0],
            "header_row_count": header_rows,
            "normalized_logical_columns": columns,
            "normalized_logical_column_names": [column["name"] for column in columns],
            "data_row_count": len(parsed.rows) - header_rows,
            "physical_column_count": len(parsed.rows[0]),
            "logical_column_count": len(columns),
            "html_merges": parsed.merges,
            "parse_issues": parsed.issues,
        },
        "database_suitability": suitability,
        "properties": properties,
        "records": records,
        "filtered_rows": filtered_rows,
        "degradations": degradations,
        "warnings": warnings,
        "warnings_and_fallbacks": warnings + [
            f"{item['property']}: {item['reason']} -> rich_text" for item in degradations
        ],
        "view_column_order": view_columns,
        "stable_sort_rule": {
            "property": row_property,
            "direction": "ascending",
            "value_origin": "source_row_number",
        },
        "default_table_view": view,
        "expected_record_count": len(records),
    }
    checks = validate_plan(plan)
    plan["validation"] = {"valid": True, "checks": checks}
    plan["prewrite_validation"] = checks
    return plan


def write_plan(plan: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "database_plan.json"
    output_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a deterministic Notion database plan.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--database-name")
    parser.add_argument("--source-file")
    parser.add_argument("--table-page", type=int)
    parser.add_argument("--table-idx", type=int)
    args = parser.parse_args()
    try:
        plan = build_plan(
            args.source,
            database_name=args.database_name,
            source_file=args.source_file,
            table_page_num=args.table_page,
            table_idx=args.table_idx,
        )
    except TableSelectionRequired as exc:
        print(
            json.dumps(
                {
                    "error": "table_selection_required",
                    "message": str(exc),
                    "candidates": exc.candidates,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2) from exc
    output_path = write_plan(plan, args.output_dir)
    print(
        json.dumps(
            {
                "plan": output_path.as_posix(),
                "database_name": plan["database_name"],
                "records": plan["expected_record_count"],
                "table": {
                    "page_num": plan["source_table"]["page_num"],
                    "idx": plan["source_table"]["idx"],
                },
                "valid": plan["validation"]["valid"],
                "recommended": plan["database_suitability"]["recommended"],
                "requires_confirmation": plan["database_suitability"]["requires_confirmation"],
                "warning_count": len(plan["warnings"]),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
