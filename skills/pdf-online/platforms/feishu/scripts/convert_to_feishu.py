#!/usr/bin/env python3
"""Generate a Feishu-compatible Markdown draft from SoMark outputs."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable


HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
IMAGE_RE = re.compile(
    r"!\[(?P<alt>.*?)\]\((?P<url>https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)",
    re.DOTALL,
)
FENCE_LINE_RE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
CE_COMMAND_RE = re.compile(r"\\ce\{(?:[^{}]|\{[^{}]*\})*\}")
CE_BLOCK_RE = re.compile(
    r"\$\$\s*(?P<command>\\ce\{(?:[^{}]|\{[^{}]*\})*\})\s*\$\$",
    re.DOTALL,
)
CE_INLINE_RE = re.compile(
    r"(?<!\$)\$(?P<command>\\ce\{(?:[^{}]|\{[^{}]*\})*\})\$(?!\$)",
    re.DOTALL,
)
FORMULA_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+\$(?P<body>[^$\r\n]+)\$[ \t]*$",
    re.MULTILINE,
)
HEADING_LINE_RE = re.compile(
    r"^(?P<marks>#{1,6})[ \t]+(?P<body>[^\r\n]+?)[ \t]*$",
    re.MULTILINE,
)
INLINE_FORMULA_RE = re.compile(
    r"(?<!\$)\$(?!\$)(?P<body>[^\r\n$]+?)\$(?!\$)"
)
FORMULA_TOKEN_RE = re.compile(
    r"\$\$(?P<block>.+?)\$\$|(?<!\$)\$(?!\$)(?P<inline>[^\r\n$]+?)\$(?!\$)",
    re.DOTALL,
)
TABLE_CELL_RE = re.compile(
    r"(?P<open><t[dh]\b[^>]*>)(?P<body>.*?)(?P<close></t[dh]\s*>)",
    re.IGNORECASE | re.DOTALL,
)
PROTECTED_TABLE_TOKEN_RE = re.compile(
    r"<[^>]+>|\$\$.*?\$\$|(?<!\$)\$(?!\$)[^\r\n$]*\$(?!\$)",
    re.DOTALL,
)
BARE_SUPERSCRIPT_RE = re.compile(r"(?<![\\$])\^\{(?P<body>[^{}\r\n]+)\}")
PROTECTED_SUPERSCRIPT_RE = re.compile(
    r"<table\b[^>]*>.*?</table\s*>|"
    r"```.*?```|~~~.*?~~~|`[^`\r\n]*`|"
    r"\$\$.*?\$\$|(?<!\$)\$(?!\$)[^\r\n$]*\$(?!\$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class TableCell:
    text: str
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False
    image_urls: list[str] = field(default_factory=list)


class SoMarkTableParser(HTMLParser):
    """Read a SoMark HTML table without changing its source markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[TableCell]] = []
        self.current_row: list[TableCell] | None = None
        self.current_cell: TableCell | None = None
        self.cell_parts: list[str] = []
        self.table_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value for key, value in attrs}
        if tag == "table":
            self.table_depth += 1
            return
        if self.table_depth != 1:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = TableCell(
                text="",
                rowspan=_positive_int(attr_map.get("rowspan")),
                colspan=_positive_int(attr_map.get("colspan")),
                is_header=tag == "th",
            )
            self.cell_parts = []
        elif self.current_cell is not None:
            if tag == "br":
                self.cell_parts.append("\n")
            elif tag == "img":
                alt = _single_line(attr_map.get("alt") or "")
                if alt:
                    self.cell_parts.append(alt)
                image_reference = html.unescape(
                    attr_map.get("src") or attr_map.get("href") or ""
                ).strip()
                if image_reference:
                    self.current_cell.image_urls.append(image_reference)
            elif tag in {"p", "div", "li"} and self.cell_parts:
                self.cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            return
        if self.table_depth != 1:
            return
        if tag in {"td", "th"} and self.current_cell is not None:
            self.current_cell.text = "".join(self.cell_parts)
            if self.current_row is None:
                self.current_row = []
            self.current_row.append(self.current_cell)
            self.current_cell = None
            self.cell_parts = []
        elif tag == "tr":
            if self.current_row is not None:
                self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data: str) -> None:
        if self.table_depth == 1 and self.current_cell is not None:
            self.cell_parts.append(data)


def _positive_int(value: str | None) -> int:
    try:
        parsed = int(value or "1")
    except ValueError:
        return 1
    return parsed if parsed > 0 else 1


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_blocks(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("type"), str):
            yield data
        for value in data.values():
            yield from _iter_blocks(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_blocks(item)


def unwrap_somark_json(data: Any) -> Any:
    """Accept both raw parsed JSON and the official full API response."""
    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        return data
    if not isinstance(data, dict):
        return data
    task_data = data.get("data") if isinstance(data.get("data"), dict) else data
    result = task_data.get("result") if isinstance(task_data, dict) else None
    outputs = result.get("outputs") if isinstance(result, dict) else None
    payload = outputs.get("json") if isinstance(outputs, dict) else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return data
    return payload if isinstance(payload, dict) else data


def has_visible_json_block(data: Any) -> bool:
    for block in _iter_blocks(data):
        if block.get("display") is False:
            continue
        content = block.get("content")
        image_url = block.get("img_url")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(image_url, str) and image_url.strip():
            return True
    return False


def extract_code_specs(data: Any) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for block in _iter_blocks(data):
        if block.get("type") != "code":
            continue
        content = block.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        image_match = re.search(r"\s+!\[[^\]]*\]\(", content)
        if image_match:
            content = content[: image_match.start()]
        language = block.get("code_language")
        specs.append((content.strip(), language.strip() if isinstance(language, str) else ""))
    return specs


def _block_content(block: dict[str, Any]) -> str:
    content = block.get("content")
    return content.strip() if isinstance(content, str) else ""


def _block_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = block.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        left, top, right, bottom = (float(raw[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _block_inside_region(candidate: dict[str, Any], region: dict[str, Any]) -> bool:
    candidate_box = _block_bbox(candidate)
    region_box = _block_bbox(region)
    if candidate_box is None or region_box is None:
        return False
    left, top, right, bottom = candidate_box
    region_left, region_top, region_right, region_bottom = region_box
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    overlap_width = max(0.0, min(right, region_right) - max(left, region_left))
    overlap_height = max(0.0, min(bottom, region_bottom) - max(top, region_top))
    return overlap_width / width >= 0.5 and overlap_height / height >= 0.8


def _footnote_text_blocks(
    blocks: list[Any], marker_index: int
) -> list[tuple[int, str]]:
    marker = blocks[marker_index]
    if not isinstance(marker, dict):
        return []
    marker_box = _block_bbox(marker)
    collected: list[tuple[int, str]] = []
    for next_index in range(marker_index + 1, len(blocks)):
        next_block = blocks[next_index]
        if not isinstance(next_block, dict) or next_block.get("type") != "text":
            break
        next_content = _block_content(next_block)
        if not next_content:
            break
        if marker_box is not None and not _block_inside_region(next_block, marker):
            break
        collected.append((next_index, next_content))
        # Without geometry, retain only the immediately following text block.
        if marker_box is None:
            break
    return collected


def extract_structural_specs(data: Any) -> list[dict[str, Any]]:
    """Extract footnotes and choices while preserving their JSON block order.

    Some SoMark results emit an empty footnote region followed by plain-text
    blocks containing the actual footnote. When geometry exists, collect only
    consecutive text blocks inside that region and merge them into one unit.
    Without geometry, conservatively retain only the first adjacent text block.
    """
    specs: list[dict[str, Any]] = []
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        pages = []

    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            continue
        blocks = page["blocks"]
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "choice":
                content = _block_content(block)
                if content:
                    specs.append(
                        {
                            "type": "choice",
                            "content": content,
                            "page_index": page_index,
                            "block_index": block_index,
                        }
                    )
                continue
            if block_type != "footnote":
                continue

            content = _block_content(block)
            if content:
                specs.append(
                    {
                        "type": "footnote",
                        "content": content,
                        "page_index": page_index,
                        "block_index": block_index,
                    }
                )
                continue

            footnote_blocks = _footnote_text_blocks(blocks, block_index)
            if footnote_blocks:
                block_indexes = [index for index, _ in footnote_blocks]
                specs.append(
                    {
                        "type": "footnote",
                        "content": "\n\n".join(value for _, value in footnote_blocks),
                        "page_index": page_index,
                        "block_index": block_indexes[0],
                        "block_indexes": block_indexes,
                        "marker_index": block_index,
                    }
                )

    return specs


def _quote_block(content: str) -> str:
    return "\n".join(">" if not line.strip() else f"> {line}" for line in content.splitlines())


def _footnote_quote(content: str, note_number: int) -> str:
    return _quote_block(f"[尾注 {note_number}] {content}")


def _unordered_list(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return "\n".join(
        line if re.match(r"^[-*+]\s+", line) else f"- {line}" for line in lines
    )


def transform_structural_elements(
    markdown: str, specs: list[dict[str, Any]]
) -> tuple[str, dict[str, int]]:
    """Map SoMark footnotes and choices to Feishu Markdown primitives."""
    converted = markdown
    stats = {
        "footnotes_quoted": 0,
        "choices_listed": 0,
    }
    next_note_number = 1
    for spec in specs:
        source = spec["content"]
        if source not in converted:
            continue
        if spec["type"] == "footnote":
            note_number = int(spec.get("note_number") or next_note_number)
            replacement = _footnote_quote(source, note_number)
            next_note_number = max(next_note_number, note_number + 1)
            stats["footnotes_quoted"] += 1
        else:
            replacement = _unordered_list(source)
            stats["choices_listed"] += 1
        converted = converted.replace(source, replacement, 1)
    return converted, stats


def _map_outside_fences(
    markdown: str, transform: Callable[[str], tuple[str, dict[str, int]]]
) -> tuple[str, dict[str, int]]:
    lines = markdown.splitlines(keepends=True)
    output: list[str] = []
    plain_buffer: list[str] = []
    code_buffer: list[str] = []
    open_marker: str | None = None
    totals: dict[str, int] = {}

    def merge_stats(stats: dict[str, int]) -> None:
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value

    def flush_plain() -> None:
        if not plain_buffer:
            return
        converted, stats = transform("".join(plain_buffer))
        output.append(converted)
        merge_stats(stats)
        plain_buffer.clear()

    for line in lines:
        match = FENCE_LINE_RE.match(line.rstrip("\r\n"))
        marker = match.group("marker") if match else None
        if open_marker is None:
            if marker:
                flush_plain()
                open_marker = marker
                code_buffer.append(line)
            else:
                plain_buffer.append(line)
        else:
            code_buffer.append(line)
            if marker and marker[0] == open_marker[0] and len(marker) >= len(open_marker):
                output.append("".join(code_buffer))
                code_buffer.clear()
                open_marker = None

    flush_plain()
    if code_buffer:
        output.append("".join(code_buffer))
    return "".join(output), totals


def _map_outside_fences_with_records(
    markdown: str,
    transform: Callable[
        [str], tuple[str, dict[str, int], list[dict[str, Any]]]
    ],
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    lines = markdown.splitlines(keepends=True)
    output: list[str] = []
    plain_buffer: list[str] = []
    code_buffer: list[str] = []
    open_marker: str | None = None
    totals: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    def flush_plain() -> None:
        if not plain_buffer:
            return
        converted, stats, new_records = transform("".join(plain_buffer))
        output.append(converted)
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value
        if new_records and "table_index" in new_records[0]:
            offset = sum("table_index" in record for record in records)
            for record in new_records:
                record["table_index"] += offset
        if new_records and "image_index" in new_records[0]:
            offset = sum("image_index" in record for record in records)
            for record in new_records:
                record["image_index"] += offset
        records.extend(new_records)
        plain_buffer.clear()

    for line in lines:
        match = FENCE_LINE_RE.match(line.rstrip("\r\n"))
        marker = match.group("marker") if match else None
        if open_marker is None:
            if marker:
                flush_plain()
                open_marker = marker
                code_buffer.append(line)
            else:
                plain_buffer.append(line)
        else:
            code_buffer.append(line)
            if marker and marker[0] == open_marker[0] and len(marker) >= len(open_marker):
                output.append("".join(code_buffer))
                code_buffer.clear()
                open_marker = None

    flush_plain()
    if code_buffer:
        output.append("".join(code_buffer))
    return "".join(output), totals, records


def _formula_specs(text: str) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for match in FORMULA_TOKEN_RE.finditer(text):
        content = match.group("block") or match.group("inline") or ""
        depth = 0
        balanced = True
        for character in content:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
        if content.strip() and balanced and depth == 0:
            specs.append({"marker": match.group(0), "content": content.strip()})
    return specs


def _repair_bare_table_superscripts(source: str) -> tuple[str, int]:
    repaired = 0

    def repair_plain(fragment: str) -> str:
        nonlocal repaired

        def replace(match: re.Match[str]) -> str:
            nonlocal repaired
            repaired += 1
            return f"${match.group(0)}$"

        return BARE_SUPERSCRIPT_RE.sub(replace, fragment)

    def repair_cell(match: re.Match[str]) -> str:
        body = match.group("body")
        output: list[str] = []
        position = 0
        for protected in PROTECTED_TABLE_TOKEN_RE.finditer(body):
            output.append(repair_plain(body[position : protected.start()]))
            output.append(protected.group(0))
            position = protected.end()
        output.append(repair_plain(body[position:]))
        return f"{match.group('open')}{''.join(output)}{match.group('close')}"

    return TABLE_CELL_RE.sub(repair_cell, source), repaired


def transform_bare_superscripts(source: str) -> tuple[str, dict[str, int]]:
    """Wrap safe bare ``^{...}`` runs as inline formulas outside protected spans."""
    repaired = 0

    def repair_plain(fragment: str) -> str:
        nonlocal repaired

        def replace(match: re.Match[str]) -> str:
            nonlocal repaired
            repaired += 1
            return f"${match.group(0)}$"

        return BARE_SUPERSCRIPT_RE.sub(replace, fragment)

    output: list[str] = []
    position = 0
    for protected in PROTECTED_SUPERSCRIPT_RE.finditer(source):
        output.append(repair_plain(source[position : protected.start()]))
        output.append(protected.group(0))
        position = protected.end()
    output.append(repair_plain(source[position:]))
    return "".join(output), {"bare_superscripts_wrapped": repaired}


def build_table_plan(source: str, table_index: int) -> dict[str, Any]:
    parser = SoMarkTableParser()
    parser.feed(source)
    parser.close()
    if not parser.rows or not any(parser.rows):
        raise ValueError("HTML table contained no rows or cells")
    occupied: set[tuple[int, int]] = set()
    cells: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    row_count = len(parser.rows)
    column_count = 0
    merged_cells = 0
    nominal_width = max(
        (sum(cell.colspan for cell in row) for row in parser.rows),
        default=0,
    )

    for row_index, row in enumerate(parser.rows):
        row_width = sum(cell.colspan for cell in row)
        if nominal_width and row_width >= nominal_width:
            # SoMark occasionally emits a full data row beneath header cells
            # that also claim rowspan. Feishu keeps the complete row at column
            # zero instead of growing the visual table sideways.
            occupied = {
                position for position in occupied if position[0] != row_index
            }
        column_index = 0
        for cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            text = html.unescape(cell.text).replace("\r\n", "\n").replace("\r", "\n")
            for image_match in IMAGE_RE.finditer(text):
                image_url = image_match.group("url")
                images.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "source_url": image_url,
                        "description": _single_line(image_match.group("alt")),
                        "source_marker": f"![]({image_url})",
                    }
                )
            # Keep image descriptions in the JSON-derived plan, but use a
            # predictable empty-alt marker in the imported table cell. The
            # native-image pass removes this marker only after upload succeeds.
            text = IMAGE_RE.sub(lambda match: f"![]({match.group('url')})", text)
            formulas = _formula_specs(text)
            if formulas:
                cells.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "source_text": text.strip(),
                        "formulas": formulas,
                    }
                )
            if cell.rowspan > 1 or cell.colspan > 1:
                merged_cells += 1
            for target_row in range(row_index, row_index + cell.rowspan):
                for target_column in range(
                    column_index, column_index + cell.colspan
                ):
                    occupied.add((target_row, target_column))
            row_count = max(row_count, row_index + cell.rowspan)
            column_count = max(column_count, column_index + cell.colspan)
            column_index += cell.colspan

    return {
        "table_index": table_index,
        "expected_rows": row_count,
        "expected_columns": column_count,
        "merged_cells": merged_cells,
        "cells": cells,
        "images": images,
    }


def _sanitize_conflicting_header_rowspan(source: str) -> tuple[str, int]:
    parser = SoMarkTableParser()
    parser.feed(source)
    parser.close()
    if len(parser.rows) < 2:
        return source, 0
    nominal_width = max(
        (sum(cell.colspan for cell in row) for row in parser.rows),
        default=0,
    )
    first_width = sum(cell.colspan for cell in parser.rows[0])
    second_width = sum(cell.colspan for cell in parser.rows[1])
    if (
        not nominal_width
        or first_width < nominal_width
        or second_width < nominal_width
        or not any(cell.rowspan > 1 for cell in parser.rows[0])
    ):
        return source, 0
    first_row = re.search(r"<tr\b[^>]*>.*?</tr\s*>", source, re.IGNORECASE | re.DOTALL)
    if not first_row:
        return source, 0
    cleaned_row, count = re.subn(
        r"\s+rowspan\s*=\s*(['\"])\d+\1",
        "",
        first_row.group(0),
        flags=re.IGNORECASE,
    )
    if not count:
        return source, 0
    return source[: first_row.start()] + cleaned_row + source[first_row.end() :], count


def collect_table_plans(
    markdown: str,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    stats = {
        "html_tables_preserved": 0,
        "merged_table_cells_preserved": 0,
        "table_formula_cells_queued": 0,
        "table_formulas_queued": 0,
        "table_images_queued": 0,
        "table_image_descriptions_queued": 0,
        "bare_table_superscripts_wrapped": 0,
        "conflicting_header_rowspans_removed": 0,
        "html_tables_failed": 0,
    }
    plans: list[dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        sanitized, removed_rowspans = _sanitize_conflicting_header_rowspan(
            match.group(0)
        )
        sanitized, repaired_superscripts = _repair_bare_table_superscripts(sanitized)
        try:
            plan = build_table_plan(sanitized, len(plans))
        except (ValueError, RuntimeError):
            stats["html_tables_failed"] += 1
            return match.group(0)
        plans.append(plan)
        stats["html_tables_preserved"] += 1
        stats["merged_table_cells_preserved"] += plan["merged_cells"]
        stats["table_formula_cells_queued"] += len(plan["cells"])
        stats["table_formulas_queued"] += sum(
            len(cell["formulas"]) for cell in plan["cells"]
        )
        stats["table_images_queued"] += len(plan["images"])
        stats["table_image_descriptions_queued"] += sum(
            bool(image.get("description")) for image in plan["images"]
        )
        stats["bare_table_superscripts_wrapped"] += repaired_superscripts
        stats["conflicting_header_rowspans_removed"] += removed_rowspans
        # Historical live runs show Feishu recognizes a table-cell image when
        # the Markdown image is an isolated paragraph inside <td>/<th>. Keep
        # that native import route first; the Block API plan is a fallback.
        return IMAGE_RE.sub(
            lambda image_match: f"\n\n![]({image_match.group('url')})\n\n",
            sanitized,
        )

    return HTML_TABLE_RE.sub(replace, markdown), stats, plans


def transform_images(
    markdown: str,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    stats = {
        "images_seen": 0,
        "image_descriptions_queued": 0,
        "multiline_image_descriptions_flattened": 0,
    }
    plans: list[dict[str, Any]] = []

    def nearest_text(fragment: str, *, before: bool) -> str:
        lines = fragment.splitlines()
        candidates = reversed(lines) if before else iter(lines)
        for line in candidates:
            candidate = IMAGE_RE.sub(" ", line)
            candidate = re.sub(r"<[^>]+>", " ", candidate)
            candidate = re.sub(r"^\s*#{1,6}\s+", "", candidate)
            candidate = re.sub(r"^\s*>\s?", "", candidate)
            candidate = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", candidate)
            candidate = _single_line(html.unescape(candidate))
            if candidate:
                return candidate[-240:] if before else candidate[:240]
        return ""

    def replace(match: re.Match[str], *, base_offset: int) -> str:
        image_index = stats["images_seen"]
        stats["images_seen"] += 1
        alt = match.group("alt")
        caption = _single_line(alt)
        absolute_start = base_offset + match.start()
        absolute_end = base_offset + match.end()
        plans.append(
            {
                "image_index": image_index,
                "source_url": match.group("url"),
                "caption": caption,
                "previous_text": nearest_text(markdown[:absolute_start], before=True),
                "next_text": nearest_text(markdown[absolute_end:], before=False),
            }
        )
        if caption:
            stats["image_descriptions_queued"] += 1
        if "\n" in alt or "\r" in alt:
            stats["multiline_image_descriptions_flattened"] += 1
        return f"![]({match.group('url')})"

    output: list[str] = []
    position = 0
    for table_match in HTML_TABLE_RE.finditer(markdown):
        fragment = markdown[position : table_match.start()]
        output.append(
            IMAGE_RE.sub(
                lambda image_match, offset=position: replace(
                    image_match, base_offset=offset
                ),
                fragment,
            )
        )
        output.append(table_match.group(0))
        position = table_match.end()
    fragment = markdown[position:]
    output.append(
        IMAGE_RE.sub(
            lambda image_match, offset=position: replace(
                image_match, base_offset=offset
            ),
            fragment,
        )
    )
    return "".join(output), stats, plans


def _extract_ce_content(command: str) -> str:
    start = command.find("{")
    if start < 0:
        return command
    depth = 0
    for index in range(start, len(command)):
        char = command[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return command[start + 1 : index]
    return command[start + 1 :]


def _chemistry_plain_text(command: str) -> str:
    content = _extract_ce_content(command)

    def arrow(match: re.Match[str]) -> str:
        conditions = re.findall(r"\[([^\]]*)\]", match.group("conditions") or "")
        label = "；".join(item.strip() for item in conditions if item.strip())
        return f" →（{label}） " if label else " → "

    content = re.sub(
        r"->(?P<conditions>(?:\[[^\]]*\])*)",
        arrow,
        content,
    )
    content = content.replace("<=>", " ↔ ").replace("<-", " ← ")
    content = content.replace(r"\Delta", "Δ").replace(r"\uparrow", "↑")
    return re.sub(r"\s+", " ", content).strip()


def _format_chemistry_condition(condition: str) -> str:
    condition = re.sub(r"\s+", " ", condition.strip())
    condition = condition.replace("\\Delta", "Δ")
    condition = condition.replace("\\uparrow", "↑").replace("\\downarrow", "↓")
    condition = condition.replace("{", r"\{").replace("}", r"\}")
    return rf"\text{{{condition}}}"


def _format_polymer_repeat(side: str) -> str | None:
    """Render a simple bracketed repeat unit with visible chain bonds."""
    match = re.fullmatch(
        r"\[(?P<body>[^\[\]]+)\](?P<count>[A-Za-z0-9]+)", side.strip()
    )
    if not match:
        return None
    body = match.group("body").strip()
    count = match.group("count").strip()
    if not body or not re.fullmatch(r"[A-Za-z0-9(){}\-=·.]+", body):
        return None
    # Condensed ethylene-style repeat units omit the C-C bond. Add it only
    # when the whole unit is an unambiguous sequence of carbon groups.
    carbon_groups = re.fullmatch(r"C(?:H\d*)?(?:C(?:H\d*)?)+", body)
    if carbon_groups and "-" not in body and "=" not in body:
        body = re.sub(r"(?<=\d)(?=C)", "-", body)
    formatted = _format_chemistry_side(f"-{body}-")
    if formatted is None:
        return None
    return rf"\left[{formatted}\right]_{{{count}}}"


def _format_chemistry_side(side: str) -> str | None:
    side = re.sub(r"\s+", " ", side.strip())
    if not side:
        return None
    side = side.replace("\\Delta", "Δ")
    side = side.replace("\\uparrow", "↑").replace("\\downarrow", "↓")
    if "[" in side or "]" in side:
        return _format_polymer_repeat(side)
    if "\\" in side or "^" in side or "_" in side:
        return None
    if not re.fullmatch(
        r"[A-Za-z0-9\s+\-=(){}·.↑↓°%Δ]+",
        side,
    ):
        return None

    side = side.replace("{", "(").replace("}", ")")
    side = re.sub(r"(?<=[A-Za-z)])(\d+)", r"_{\1}", side)
    side = re.sub(r"(?<=\))n\b", r"_n", side)
    side = side.replace("·", r"\cdot ")
    side = side.replace("↑", r"\uparrow ").replace("↓", r"\downarrow ")
    side = side.replace("°", r"^\circ ")
    return rf"\mathrm{{{side.strip()}}}"


def _chemistry_katex(command: str) -> str | None:
    content = _extract_ce_content(command).strip()
    arrow_matches = list(
        re.finditer(
            r"(?P<arrow><=>|<->|<-|->)(?P<conditions>(?:\[[^\]]*\])*)",
            content,
        )
    )
    if len(arrow_matches) > 1:
        return None
    if not arrow_matches:
        return _format_chemistry_side(content)

    match = arrow_matches[0]
    left = _format_chemistry_side(content[: match.start()])
    right = _format_chemistry_side(content[match.end() :])
    if left is None or right is None:
        return None

    conditions = [
        _format_chemistry_condition(value)
        for value in re.findall(r"\[([^\]]*)\]", match.group("conditions"))
        if value.strip()
    ]
    arrow = match.group("arrow")
    if arrow == "->":
        if len(conditions) >= 2:
            arrow_latex = (
                rf"\xrightarrow[{'; '.join(conditions[1:])}]"
                rf"{{{conditions[0]}}}"
            )
        elif conditions:
            arrow_latex = rf"\xrightarrow{{{conditions[0]}}}"
        else:
            arrow_latex = r"\rightarrow"
    elif arrow == "<-":
        if conditions:
            arrow_latex = rf"\xleftarrow{{{conditions[0]}}}"
        else:
            arrow_latex = r"\leftarrow"
    else:
        if conditions:
            return None
        arrow_latex = r"\rightleftharpoons"
    return f"{left} {arrow_latex} {right}"


def transform_chemistry(markdown: str) -> tuple[str, dict[str, int]]:
    stats = {
        "chemistry_formulas_converted": 0,
        "chemistry_block_formulas_converted": 0,
        "chemistry_inline_formulas_converted": 0,
        "chemistry_formulas_degraded": 0,
        "chemistry_block_formulas_degraded": 0,
        "chemistry_inline_formulas_degraded": 0,
    }

    def replace_block(match: re.Match[str]) -> str:
        katex = _chemistry_katex(match.group("command"))
        if katex is not None:
            stats["chemistry_formulas_converted"] += 1
            stats["chemistry_block_formulas_converted"] += 1
            return f"$$\n{katex}\n$$"
        stats["chemistry_formulas_degraded"] += 1
        stats["chemistry_block_formulas_degraded"] += 1
        return _chemistry_plain_text(match.group("command"))

    def replace_inline(match: re.Match[str]) -> str:
        katex = _chemistry_katex(match.group("command"))
        if katex is not None:
            stats["chemistry_formulas_converted"] += 1
            stats["chemistry_inline_formulas_converted"] += 1
            return f"${katex}$"
        stats["chemistry_formulas_degraded"] += 1
        stats["chemistry_inline_formulas_degraded"] += 1
        return _chemistry_plain_text(match.group("command"))

    markdown = CE_BLOCK_RE.sub(replace_block, markdown)
    markdown = CE_INLINE_RE.sub(replace_inline, markdown)

    def replace_formula_content(match: re.Match[str]) -> str:
        context = "block" if match.group("block") is not None else "inline"
        body = match.group(context)

        def replace_nested(command_match: re.Match[str]) -> str:
            katex = _chemistry_katex(command_match.group(0))
            if katex is not None:
                stats["chemistry_formulas_converted"] += 1
                stats[f"chemistry_{context}_formulas_converted"] += 1
                return katex
            stats["chemistry_formulas_degraded"] += 1
            stats[f"chemistry_{context}_formulas_degraded"] += 1
            return _chemistry_plain_text(command_match.group(0))

        converted = CE_COMMAND_RE.sub(replace_nested, body)
        delimiter = "$$" if context == "block" else "$"
        return f"{delimiter}{converted}{delimiter}"

    markdown = FORMULA_TOKEN_RE.sub(replace_formula_content, markdown)

    def replace_remaining(match: re.Match[str]) -> str:
        katex = _chemistry_katex(match.group(0))
        if katex is not None:
            stats["chemistry_formulas_converted"] += 1
            stats["chemistry_inline_formulas_converted"] += 1
            return f"${katex}$"
        stats["chemistry_formulas_degraded"] += 1
        return _chemistry_plain_text(match.group(0))

    markdown = CE_COMMAND_RE.sub(replace_remaining, markdown)
    return markdown, stats


def transform_formula_operators(markdown: str) -> tuple[str, dict[str, int]]:
    """Escape raw comparison operators that Feishu's importer treats as HTML."""
    stats = {
        "formula_comparison_operators_escaped": 0,
        "formulas_with_comparison_operators_escaped": 0,
    }

    def replace(match: re.Match[str]) -> str:
        context = "block" if match.group("block") is not None else "inline"
        body = match.group(context)
        escaped, count = re.subn(r"<", r"\\lt ", body)
        escaped, greater_count = re.subn(r">", r"\\gt ", escaped)
        count += greater_count
        if count:
            stats["formula_comparison_operators_escaped"] += count
            stats["formulas_with_comparison_operators_escaped"] += 1
        delimiter = "$$" if context == "block" else "$"
        return f"{delimiter}{escaped}{delimiter}"

    return FORMULA_TOKEN_RE.sub(replace, markdown), stats


def collect_formula_audit_plan(markdown: str) -> dict[str, Any]:
    """Record expected equations and targeted rich-text repairs after preview."""
    expected: list[dict[str, Any]] = []
    operator_paragraphs: list[dict[str, Any]] = []
    masked = HTML_TABLE_RE.sub("", markdown)

    for match in FORMULA_TOKEN_RE.finditer(masked):
        context = "block" if match.group("block") is not None else "inline"
        expected.append(
            {
                "formula_index": len(expected),
                "content": match.group(context).strip(),
                "display": context == "block",
            }
        )

    block_spans = [match.span() for match in re.finditer(r"\$\$.*?\$\$", masked, re.DOTALL)]
    offset = 0
    for raw_line in masked.splitlines(keepends=True):
        line_start = offset
        offset += len(raw_line)
        if any(start <= line_start < end for start, end in block_spans):
            continue
        line = raw_line.rstrip("\r\n")
        if not line.strip() or re.match(r"^\s*#{1,6}\s+", line):
            continue
        formulas = list(INLINE_FORMULA_RE.finditer(line))
        if not formulas or not any(r"\lt" in item.group("body") or r"\gt" in item.group("body") for item in formulas):
            continue
        visible_line = re.sub(r"^\s*(?:>|[-+*]|\d+[.)])\s+", "", line)
        formulas = list(INLINE_FORMULA_RE.finditer(visible_line))
        elements: list[dict[str, Any]] = []
        position = 0
        text_fragments: list[str] = []
        for formula in formulas:
            if formula.start() > position:
                text = visible_line[position : formula.start()]
                elements.append(
                    {"text_run": {"content": text, "text_element_style": {}}}
                )
                if text.strip():
                    text_fragments.append(text.strip())
            elements.append(
                {
                    "equation": {
                        "content": formula.group("body"),
                        "text_element_style": {},
                    }
                }
            )
            position = formula.end()
        if position < len(visible_line):
            text = visible_line[position:]
            elements.append({"text_run": {"content": text, "text_element_style": {}}})
            if text.strip():
                text_fragments.append(text.strip())
        operator_paragraphs.append(
            {
                "paragraph_index": len(operator_paragraphs),
                "source_text": visible_line,
                "prefix_text": text_fragments[0] if text_fragments else "",
                "suffix_text": text_fragments[-1] if text_fragments else "",
                "elements": elements,
            }
        )
    return {
        "expected_formulas": expected,
        "operator_paragraphs": operator_paragraphs,
    }


def _fenced_code_contents(markdown: str) -> list[str]:
    pattern = re.compile(
        r"^\s*(`{3,}|~{3,})[^\r\n]*\r?\n(?P<body>.*?)^\s*\1\s*$",
        re.MULTILINE | re.DOTALL,
    )
    return [match.group("body").strip() for match in pattern.finditer(markdown)]


def ensure_code_fences(
    markdown: str, code_specs: list[tuple[str, str]]
) -> tuple[str, dict[str, int], list[str]]:
    stats = {
        "json_code_blocks": len(code_specs),
        "code_blocks_recovered": 0,
        "unclosed_code_fences_closed": 0,
    }
    warnings: list[str] = []
    fenced = _fenced_code_contents(markdown)
    for code, language in code_specs:
        if any(code == existing or code in existing for existing in fenced):
            continue
        if code not in markdown:
            warnings.append("JSON code block text was not found exactly in Markdown")
            continue
        fence = f"```{language}\n{code}\n```\n"
        markdown = markdown.replace(code, fence, 1)
        fenced.append(code)
        stats["code_blocks_recovered"] += 1

    open_marker: str | None = None
    for line in markdown.splitlines():
        match = FENCE_LINE_RE.match(line)
        if not match:
            continue
        marker = match.group("marker")
        if open_marker is None:
            open_marker = marker
        elif marker[0] == open_marker[0] and len(marker) >= len(open_marker):
            open_marker = None
    if open_marker is not None:
        markdown = markdown.rstrip() + "\n" + open_marker + "\n"
        stats["unclosed_code_fences_closed"] += 1
        warnings.append("An unmatched code fence was closed at end of document")
    return markdown, stats, warnings


def remove_duplicate_h1(markdown: str, title: str | None) -> tuple[str, int]:
    if not title:
        return markdown, 0
    match = re.match(r"\A(?:\ufeff)?(?:[ \t]*\r?\n)*#\s+([^\r\n]+)\r?\n", markdown)
    if not match or match.group(1).strip() != title.strip():
        return markdown, 0
    return markdown[: match.start()] + markdown[match.end() :].lstrip("\r\n"), 1


def transform_formula_wrapped_headings(
    markdown: str,
) -> tuple[str, dict[str, int]]:
    stats = {"formula_wrapped_headings_normalized": 0}

    def replace(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        if not re.match(r"^\d+(?:\.\d+)*\b", body):
            return match.group(0)
        body = re.sub(r"\\(?:qquad|quad)\b", " ", body)
        body = re.sub(r"\\(?:,|;|:|!)", " ", body)
        body = re.sub(r"\\text\{([^{}]*)\}", r"\1", body)
        body = re.sub(r"\s+", " ", body).strip()
        stats["formula_wrapped_headings_normalized"] += 1
        return f"{match.group('marks')} {body}"

    return FORMULA_HEADING_RE.sub(replace, markdown), stats


def collect_formula_heading_plans(
    markdown: str,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    stats = {
        "formula_headings_queued": 0,
        "formula_heading_formulas_queued": 0,
    }
    plans: list[dict[str, Any]] = []

    def collect(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        formulas = [
            {"marker": formula.group(0), "content": formula.group("body")}
            for formula in INLINE_FORMULA_RE.finditer(body)
        ]
        if not formulas:
            return match.group(0)
        plans.append(
            {
                "heading_index": len(plans),
                "level": len(match.group("marks")),
                "source_text": body,
                "formulas": formulas,
            }
        )
        stats["formula_headings_queued"] += 1
        stats["formula_heading_formulas_queued"] += len(formulas)
        return match.group(0)

    return HEADING_LINE_RE.sub(collect, markdown), stats, plans


def count_markdown(markdown: str) -> dict[str, int]:
    return {
        "characters": len(markdown),
        "headings": len(re.findall(r"^#{1,6}\s+\S", markdown, re.MULTILINE)),
        "images": len(IMAGE_RE.findall(markdown)),
        "html_tables": len(HTML_TABLE_RE.findall(markdown)),
        "gfm_tables": len(
            re.findall(r"^\|\s*(?:---+\s*\|\s*)+$", markdown, re.MULTILINE)
        ),
        "code_fences": len(
            re.findall(r"^\s*(?:`{3,}|~{3,})", markdown, re.MULTILINE)
        )
        // 2,
        "ce_commands": len(CE_COMMAND_RE.findall(markdown)),
        "inline_formulas": len(
            re.findall(r"(?<!\$)\$(?!\$)[^\r\n$]+\$(?!\$)", markdown)
        ),
        "block_formulas": len(re.findall(r"\$\$.*?\$\$", markdown, re.DOTALL)),
    }


def convert(
    markdown: str,
    json_data: Any,
    title: str | None,
) -> tuple[
    str,
    dict[str, int],
    list[str],
    list[dict[str, Any]],
    dict[str, Any],
]:
    stats: dict[str, int] = {}
    warnings: list[str] = []
    degradations: list[dict[str, Any]] = []

    markdown, code_stats, code_warnings = ensure_code_fences(
        markdown, extract_code_specs(json_data)
    )
    stats.update(code_stats)
    warnings.extend(code_warnings)

    structural_specs = extract_structural_specs(json_data)
    note_number = 0
    for spec in structural_specs:
        if spec["type"] == "footnote":
            note_number += 1
            spec["note_number"] = note_number
    stats["footnote_blocks_seen"] = sum(
        spec["type"] == "footnote" for spec in structural_specs
    )
    stats["choice_blocks_seen"] = sum(
        spec["type"] == "choice" for spec in structural_specs
    )
    remaining_structural_specs = list(structural_specs)

    def apply_structural_elements(fragment: str) -> tuple[str, dict[str, int]]:
        converted_fragment = fragment
        fragment_stats = {
            "footnotes_quoted": 0,
            "choices_listed": 0,
        }
        matched: list[dict[str, Any]] = []
        for spec in remaining_structural_specs:
            source = spec["content"]
            if source not in converted_fragment:
                continue
            replacement = (
                _footnote_quote(source, int(spec["note_number"]))
                if spec["type"] == "footnote"
                else _unordered_list(source)
            )
            converted_fragment = converted_fragment.replace(source, replacement, 1)
            matched.append(spec)
            key = (
                "footnotes_quoted"
                if spec["type"] == "footnote"
                else "choices_listed"
            )
            fragment_stats[key] += 1
        for spec in matched:
            remaining_structural_specs.remove(spec)
        return converted_fragment, fragment_stats

    markdown, structural_stats = _map_outside_fences(
        markdown, apply_structural_elements
    )
    stats.update(structural_stats)
    for spec in remaining_structural_specs:
        warnings.append(
            "Could not locate {} content from JSON block page {} index {} in Markdown".format(
                spec["type"], spec["page_index"], spec["block_index"]
            )
        )

    markdown, heading_stats = _map_outside_fences(
        markdown, transform_formula_wrapped_headings
    )
    stats.update(heading_stats)

    markdown, chemistry_stats = _map_outside_fences(markdown, transform_chemistry)
    stats.update(chemistry_stats)
    if chemistry_stats.get("chemistry_formulas_degraded", 0):
        degradations.append(
            {
                "type": "mhchem_formula",
                "count": chemistry_stats["chemistry_formulas_degraded"],
                "behavior": "unsupported \\\\ce syntax was converted to readable plain text",
            }
        )

    markdown, operator_stats = _map_outside_fences(
        markdown, transform_formula_operators
    )
    stats.update(operator_stats)

    markdown, table_stats, table_plans = _map_outside_fences_with_records(
        markdown, collect_table_plans
    )
    stats.update(table_stats)
    if table_stats.get("html_tables_failed", 0):
        warnings.append("At least one HTML table could not be mapped for Block API")

    markdown, superscript_stats = _map_outside_fences(
        markdown, transform_bare_superscripts
    )
    stats.update(superscript_stats)

    markdown, image_stats, image_plans = _map_outside_fences_with_records(
        markdown, transform_images
    )
    stats.update(image_stats)

    markdown, duplicate_h1_removed = remove_duplicate_h1(markdown, title)
    stats["duplicate_h1_removed"] = duplicate_h1_removed
    markdown, formula_heading_stats, formula_heading_plans = (
        _map_outside_fences_with_records(markdown, collect_formula_heading_plans)
    )
    stats.update(formula_heading_stats)
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown).strip()
    if markdown:
        markdown += "\n"
    formula_audit = collect_formula_audit_plan(markdown)
    stats["formula_audit_expected"] = len(formula_audit["expected_formulas"])
    stats["operator_formula_paragraphs_queued"] = len(
        formula_audit["operator_paragraphs"]
    )
    post_import = {
        "strategy": "feishu_block_api",
        "image_captions": image_plans,
        "table_formulas": table_plans,
        "formula_headings": formula_heading_plans,
        "formula_audit": formula_audit,
        "structural_elements": [
            {
                "type": spec["type"],
                "page_index": spec["page_index"],
                "block_index": spec["block_index"],
                **(
                    {"block_indexes": spec["block_indexes"]}
                    if "block_indexes" in spec
                    else {}
                ),
                "content": spec["content"],
                **(
                    {"note_number": spec["note_number"]}
                    if spec["type"] == "footnote"
                    else {}
                ),
                "feishu_representation": (
                    "numbered_endnote_blockquote"
                    if spec["type"] == "footnote"
                    else "unordered_list"
                ),
            }
            for spec in structural_specs
        ],
    }
    return markdown, stats, warnings, degradations, post_import


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Feishu-compatible Markdown draft from SoMark outputs."
    )
    parser.add_argument("--markdown", required=True, help="SoMark Markdown path")
    parser.add_argument("--json", dest="json_path", help="SoMark JSON path")
    parser.add_argument("--output", help="Output Markdown path")
    parser.add_argument("--manifest", help="Output manifest JSON path")
    parser.add_argument("--title", help="Requested Feishu document title")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    markdown_path = Path(args.markdown).expanduser().resolve()
    if not markdown_path.is_file():
        print(f"Markdown file not found: {markdown_path}", file=sys.stderr)
        return 1

    json_path = (
        Path(args.json_path).expanduser().resolve() if args.json_path else None
    )
    if json_path and not json_path.is_file():
        print(f"JSON file not found: {json_path}", file=sys.stderr)
        return 1

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else markdown_path.with_name(f"{markdown_path.stem}_feishu.md")
    )
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else output_path.with_suffix(".manifest.json")
    )

    markdown = markdown_path.read_text(encoding="utf-8-sig")
    json_data: Any = {}
    if json_path:
        json_data = unwrap_somark_json(
            json.loads(json_path.read_text(encoding="utf-8-sig"))
        )

    source_counts = count_markdown(markdown)
    visible_json = has_visible_json_block(json_data)
    manifest: dict[str, Any] = {
        "version": "0.2.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "document_title": args.title or markdown_path.stem,
        "source": {
            "markdown": str(markdown_path),
            "markdown_sha256": _sha256(markdown_path),
            "json": str(json_path) if json_path else None,
            "json_sha256": _sha256(json_path) if json_path else None,
            "counts": source_counts,
            "json_has_visible_block": visible_json,
        },
        "output": {
            "markdown": str(output_path),
            "manifest": str(manifest_path),
        },
        "transformations": {},
        "warnings": [],
        "degradations": [],
        "post_import": {},
    }

    converted, transformations, warnings, degradations, post_import = convert(
        markdown, json_data, args.title
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(converted, encoding="utf-8")
    manifest["status"] = "ready"
    manifest["transformations"] = transformations
    manifest["warnings"] = warnings
    manifest["degradations"] = degradations
    manifest["post_import"] = post_import
    manifest["output"]["markdown_sha256"] = _sha256(output_path)
    manifest["output"]["counts"] = count_markdown(converted)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
