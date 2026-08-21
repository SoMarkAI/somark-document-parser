#!/usr/bin/env python3
"""Deterministically convert the supported SoMark JSON subset to Notion Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SUPPORTED_TYPES = {
    "title", "text", "choice", "table", "figure", "stamp", "code", "equation", "cs",
    "figure_caption", "table_caption", "blank", "reference", "footnote", "cate",
}
FILTERED_TYPES = {"header", "footer", "sidebar", "sider"}
NFM_SPECIALS = frozenset("\\*~`$[]<>{}|^")
NUMBERED_LIST_PREFIX = re.compile(r"^(\s*\d+)\.(?=\s)")
MATH_EVIDENCE = frozenset("=^_\\{}")
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
REFERENCE_SUPERSCRIPT = re.compile(r"\^\{\[(\d+)\]\}")
BARE_SUPERSCRIPT = re.compile(r"(?<![\\$])\^\{(?P<body>[^{}\r\n]+)\}")
PROTECTED_INLINE = re.compile(
    r"`[^`\r\n]*`|\$[^$\r\n]*\$",
    re.DOTALL,
)
FOOTNOTE_LABEL = "[脚注]"
MERGE_BACKGROUND_COLORS = (
    "gray_bg",
    "blue_bg",
    "yellow_bg",
    "green_bg",
    "purple_bg",
    "orange_bg",
    "pink_bg",
    "red_bg",
    "brown_bg",
)


def escape_rich_text(value: str, *, protect_numbered_prefix: bool = False) -> str:
    """Escape NFM rich-text controls while preserving the rendered characters."""
    escaped = "".join("\\" + char if char in NFM_SPECIALS else char for char in value)
    if protect_numbered_prefix:
        escaped = NUMBERED_LIST_PREFIX.sub(r"\1\\.", escaped, count=1)
    return escaped


def content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def filename_title(source_path: Path) -> str:
    """Return the logical source filename, excluding a parser's `.source` suffix."""
    stem = source_path.stem
    return stem.removesuffix(".source")


@dataclass(frozen=True)
class SourceCell:
    content: str
    tag: str
    rowspan: int
    colspan: int


@dataclass(frozen=True)
class ParsedTable:
    rows: list[list[str]]
    source_rows: list[list[SourceCell]]
    merges: list[dict[str, Any]]
    header_row: bool
    header_rule: str
    header_inferred: bool


class SimpleTableParser(HTMLParser):
    """Parse simple table cells, preserving header and span metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[SourceCell]] = []
        self._row: list[SourceCell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_tag: str | None = None
        self._cell_rowspan = 1
        self._cell_colspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            if self._row is not None:
                raise ValueError("nested <tr> is not supported")
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None or self._cell_parts is not None:
                raise ValueError("table cells must be direct, non-nested row children")
            attr_map = {name.lower(): value for name, value in attrs}
            self._cell_rowspan = self._positive_span(attr_map.get("rowspan"), "rowspan")
            self._cell_colspan = self._positive_span(attr_map.get("colspan"), "colspan")
            self._cell_tag = tag
            self._cell_parts = []
        elif tag not in {"table", "tbody", "thead", "tfoot"}:
            raise ValueError(f"unsupported table tag: {tag}")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            if self._row is None or self._cell_parts is None:
                raise ValueError(f"unexpected </{tag}>")
            self._row.append(
                SourceCell(
                    content="".join(self._cell_parts),
                    tag=self._cell_tag or tag,
                    rowspan=self._cell_rowspan,
                    colspan=self._cell_colspan,
                )
            )
            self._cell_parts = None
            self._cell_tag = None
        elif tag == "tr":
            if self._row is None or self._cell_parts is not None:
                raise ValueError("invalid </tr>")
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        elif data.strip():
            raise ValueError("non-whitespace text outside table cells is not supported")

    @staticmethod
    def _positive_span(value: str | None, name: str) -> int:
        if value is None:
            return 1
        try:
            span = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if span < 1:
            raise ValueError(f"{name} must be a positive integer")
        return span

    def finish(self) -> list[list[SourceCell]]:
        self.close()
        if self._row is not None or self._cell_parts is not None or not self.rows:
            raise ValueError("incomplete or empty table")
        return self.rows


def parse_simple_table(source: str) -> list[list[str]]:
    """Compatibility helper returning the rectangular degraded cell matrix."""
    return parse_table(source).rows


def parse_table(source: str) -> ParsedTable:
    parser = SimpleTableParser()
    parser.feed(source)
    source_rows = parser.finish()
    occupied: dict[tuple[int, int], bool] = {}
    values: dict[tuple[int, int], str] = {}
    merges: list[dict[str, Any]] = []
    max_row = len(source_rows)
    max_col = 0

    for row_index, source_row in enumerate(source_rows, start=1):
        column_index = 1
        for cell in source_row:
            while occupied.get((row_index, column_index), False):
                column_index += 1
            for target_row in range(row_index, row_index + cell.rowspan):
                for target_col in range(column_index, column_index + cell.colspan):
                    if occupied.get((target_row, target_col), False):
                        raise ValueError("overlapping merged table cells are not supported")
                    occupied[(target_row, target_col)] = True
                    values[(target_row, target_col)] = ""
            values[(row_index, column_index)] = cell.content
            if cell.rowspan > 1 or cell.colspan > 1:
                merges.append(
                    {
                        "start_row": row_index,
                        "start_column": column_index,
                        "rowspan": cell.rowspan,
                        "colspan": cell.colspan,
                        "original_content": cell.content,
                    }
                )
            max_row = max(max_row, row_index + cell.rowspan - 1)
            max_col = max(max_col, column_index + cell.colspan - 1)
            column_index += cell.colspan

    rows = [[values.get((row, col), "") for col in range(1, max_col + 1)] for row in range(1, max_row + 1)]
    first_source_row = source_rows[0]
    if first_source_row and all(cell.tag == "th" for cell in first_source_row):
        header_row, header_rule, header_inferred = True, "explicit_all_th_first_row", False
    elif any(cell.tag == "th" for row in source_rows for cell in row):
        header_row, header_rule, header_inferred = False, "mixed_or_nonfirst_th_is_not_reliably_mappable", False
    else:
        first_values = rows[0]
        later_values = {value for row in rows[1:] for value in row if value}
        header_row = bool(len(rows) > 1 and all(first_values) and not any(value in later_values for value in first_values))
        header_rule = "td_first_row_nonempty_and_values_absent_from_data_rows" if header_row else "no_reliable_td_header_evidence"
        header_inferred = header_row
    return ParsedTable(rows, source_rows, merges, header_row, header_rule, header_inferred)


def merge_regions_touch(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return true when two merge rectangles touch by an edge or corner."""
    left_row_end = left["start_row"] + left["rowspan"] - 1
    left_column_end = left["start_column"] + left["colspan"] - 1
    right_row_end = right["start_row"] + right["rowspan"] - 1
    right_column_end = right["start_column"] + right["colspan"] - 1
    row_gap = max(
        right["start_row"] - left_row_end - 1,
        left["start_row"] - right_row_end - 1,
        0,
    )
    column_gap = max(
        right["start_column"] - left_column_end - 1,
        left["start_column"] - right_column_end - 1,
        0,
    )
    return row_gap == 0 and column_gap == 0


def assign_merge_backgrounds(merges: list[dict[str, Any]]) -> dict[int, str]:
    """Color touching regions differently while greedily reusing the smallest palette."""
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(merges))}
    for left_index, left in enumerate(merges):
        for right_index in range(left_index + 1, len(merges)):
            if merge_regions_touch(left, merges[right_index]):
                adjacency[left_index].add(right_index)
                adjacency[right_index].add(left_index)

    color_indices: dict[int, int] = {}
    uncolored = set(adjacency)
    while uncolored:
        region_index = max(
            uncolored,
            key=lambda index: (
                len({color_indices[neighbor] for neighbor in adjacency[index] if neighbor in color_indices}),
                len(adjacency[index]),
                -merges[index]["start_row"],
                -merges[index]["start_column"],
                -index,
            ),
        )
        forbidden = {
            color_indices[neighbor]
            for neighbor in adjacency[region_index]
            if neighbor in color_indices
        }
        available = next(
            (index for index in range(len(MERGE_BACKGROUND_COLORS)) if index not in forbidden),
            None,
        )
        if available is None:
            raise ValueError("merged-cell adjacency requires more background colors than Notion supports")
        color_indices[region_index] = available
        uncolored.remove(region_index)
    return {
        region_index: MERGE_BACKGROUND_COLORS[color_index]
        for region_index, color_index in color_indices.items()
    }


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def extract_pages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept extracted pages or the supported official SoMark response wrappers."""
    paths = (
        ("pages",),
        ("data", "result", "outputs", "json", "pages"),
        ("result", "outputs", "json", "pages"),
        ("outputs", "json", "pages"),
    )
    pages = None
    for path in paths:
        candidate = _nested(payload, *path)
        if candidate is not None:
            pages = candidate
            break
    if not isinstance(pages, list):
        raise ValueError("input must be an official SoMark response or an object containing pages")
    for position, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            raise ValueError(f"page at position {position} must contain a blocks array")
    return pages


def load_blocks(source_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    blocks: list[dict[str, Any]] = []
    for page_position, page in enumerate(extract_pages(payload)):
        page_num = page.get("page_num", page_position)
        page_blocks = sorted(page["blocks"], key=lambda block: block["idx"])
        indices = [block["idx"] for block in page_blocks]
        if len(indices) != len(set(indices)):
            raise ValueError(f"source idx values must be unique within page {page_num}")
        for block in page_blocks:
            enriched = dict(block)
            enriched["_source_page_num"] = page_num
            blocks.append(enriched)
    unsupported = sorted({block["type"] for block in blocks} - SUPPORTED_TYPES - FILTERED_TYPES)
    if unsupported:
        raise ValueError(f"unsupported source types: {', '.join(unsupported)}")
    return blocks


def source_identity(block: dict[str, Any]) -> tuple[Any, Any]:
    return block["_source_page_num"], block["idx"]


def valid_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        return None
    left, top, right, bottom = (float(item) for item in value)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def bbox_containment_ratio(
    child: tuple[float, float, float, float],
    parent: tuple[float, float, float, float],
) -> float:
    child_left, child_top, child_right, child_bottom = child
    parent_left, parent_top, parent_right, parent_bottom = parent
    intersection_width = max(0.0, min(child_right, parent_right) - max(child_left, parent_left))
    intersection_height = max(0.0, min(child_bottom, parent_bottom) - max(child_top, parent_top))
    child_area = (child_right - child_left) * (child_bottom - child_top)
    return intersection_width * intersection_height / child_area


def resolve_footnotes(
    blocks: list[dict[str, Any]],
) -> tuple[
    dict[tuple[Any, Any], dict[str, Any]],
    dict[tuple[Any, Any], tuple[Any, Any]],
]:
    """Resolve footnote containers and prevent their child text from being emitted twice."""
    groups: dict[tuple[Any, Any], dict[str, Any]] = {}
    consumed_texts: dict[tuple[Any, Any], tuple[Any, Any]] = {}
    for block_position, block in enumerate(blocks):
        if block["type"] != "footnote":
            continue
        identity = source_identity(block)
        direct_content = block.get("content", "")
        if direct_content.strip():
            groups[identity] = {
                "content": direct_content,
                "content_origin": "footnote_content",
                "child_source_indices": [],
            }
            continue

        parent_bbox = valid_bbox(block.get("bbox"))
        children: list[dict[str, Any]] = []
        if parent_bbox is not None:
            for candidate in blocks[block_position + 1 :]:
                if candidate["_source_page_num"] != block["_source_page_num"]:
                    break
                candidate_identity = source_identity(candidate)
                if candidate["type"] != "text" or candidate_identity in consumed_texts:
                    continue
                child_bbox = valid_bbox(candidate.get("bbox"))
                if child_bbox is None or bbox_containment_ratio(child_bbox, parent_bbox) < 0.8:
                    continue
                if not candidate.get("content", "").strip():
                    continue
                children.append(candidate)
                consumed_texts[candidate_identity] = identity

        groups[identity] = {
            "content": "\n".join(child["content"] for child in children),
            "content_origin": "contained_text_blocks" if children else "unresolved_empty_region",
            "child_source_indices": [child["idx"] for child in children],
        }
    return groups, consumed_texts


def rich_text_with_inline_formulas(
    value: str, *, protect_numbered_prefix: bool = False, accept_standard_formulas: bool = False
) -> tuple[str, list[dict[str, str]]]:
    """Convert reliable $...$ formulas and escape all surrounding rich text."""
    protected_output: list[str] = []
    protected_cursor = 0
    for protected in PROTECTED_INLINE.finditer(value):
        protected_output.append(
            BARE_SUPERSCRIPT.sub(
                lambda match: f"${match.group(0)}$",
                value[protected_cursor : protected.start()],
            )
        )
        protected_output.append(protected.group(0))
        protected_cursor = protected.end()
    protected_output.append(
        BARE_SUPERSCRIPT.sub(
            lambda match: f"${match.group(0)}$", value[protected_cursor:]
        )
    )
    value = "".join(protected_output)
    output: list[str] = []
    degradations: list[dict[str, str]] = []
    cursor = 0
    while cursor < len(value):
        start = value.find("$", cursor)
        if start < 0:
            output.append(escape_rich_text(value[cursor:], protect_numbered_prefix=protect_numbered_prefix and cursor == 0))
            break
        output.append(escape_rich_text(value[cursor:start], protect_numbered_prefix=protect_numbered_prefix and cursor == 0))
        end = value.find("$", start + 1)
        if end < 0:
            fragment = value[start:]
            output.append(escape_rich_text(fragment))
            degradations.append({"original_text": fragment, "reason": "unpaired_dollar"})
            break
        formula = value[start + 1 : end]
        starts_like_currency = start + 1 < len(value) and value[start + 1].isdigit()
        has_explicit_latex_command = bool(re.search(r"\\[A-Za-z]+", formula))
        closing_has_formula_boundary = (
            end + 1 == len(value)
            or value[end + 1].isspace()
            or not value[end + 1].isalnum() and value[end + 1] != "_"
        )
        if starts_like_currency and not closing_has_formula_boundary and not has_explicit_latex_command:
            output.append(escape_rich_text("$"))
            cursor = start + 1
            continue
        reliable = bool(
            formula
            and formula == formula.strip()
            and "\n" not in formula
            and "`" not in formula
            and "$" not in formula
            and (accept_standard_formulas or any(char in MATH_EVIDENCE for char in formula))
        )
        if reliable:
            output.append(f"$`{formula}`$")
        else:
            fragment = value[start : end + 1]
            output.append(escape_rich_text(fragment))
            degradations.append({"original_text": fragment, "reason": "insufficient_math_evidence"})
        cursor = end + 1
    return "".join(output), degradations


def convert_table_cell(
    value: str, *, next_image_number: int
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    """Render table rich text and replace unsupported cell images with numbered links."""
    images: list[dict[str, Any]] = []

    def replace_image(match: re.Match[str]) -> str:
        number = next_image_number + len(images)
        caption = match.group(1)
        images.append(
            {
                "number": number,
                "url": match.group(2),
                "source_alt": caption,
                "caption": caption,
                "caption_empty": caption == "",
            }
        )
        return f"\ue000{number}\ue001"

    without_images = MARKDOWN_IMAGE.sub(replace_image, value).strip()
    with_reference_formulas = REFERENCE_SUPERSCRIPT.sub(r"$^{[\1]}$", without_images)
    converted, degradations = rich_text_with_inline_formulas(
        with_reference_formulas, accept_standard_formulas=True
    )
    for image in images:
        label = f"表中图{image['number']}"
        image["label"] = label
        converted = converted.replace(
            f"\ue000{image['number']}\ue001", f"[{label}]({image['url']})"
        )
    return converted, images, degradations


def table_to_nfm(
    table: ParsedTable,
    *,
    fill_merged_cells: bool = False,
    color_merged_cells: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    header = ' header-row="true"' if table.header_row else ""
    lines = [f"<table{header}>"]
    degradations: list[dict[str, Any]] = []
    table_images: list[dict[str, Any]] = []
    rows = [list(row) for row in table.rows]
    backgrounds = assign_merge_backgrounds(table.merges) if color_merged_cells else {}
    cell_backgrounds: dict[tuple[int, int], str] = {}
    filled_from_anchor: dict[tuple[int, int], tuple[int, int]] = {}
    enhanced_regions: list[dict[str, Any]] = []
    for region_index, merge in enumerate(table.merges):
        filled_cells: list[dict[str, int]] = []
        for row_index in range(merge["start_row"], merge["start_row"] + merge["rowspan"]):
            for column_index in range(
                merge["start_column"], merge["start_column"] + merge["colspan"]
            ):
                is_anchor = (
                    row_index == merge["start_row"]
                    and column_index == merge["start_column"]
                )
                if fill_merged_cells and not is_anchor:
                    rows[row_index - 1][column_index - 1] = merge["original_content"]
                    filled_from_anchor[(row_index, column_index)] = (
                        merge["start_row"],
                        merge["start_column"],
                    )
                    filled_cells.append({"row": row_index, "column": column_index})
                if color_merged_cells:
                    cell_backgrounds[(row_index, column_index)] = backgrounds[region_index]
        enhanced_regions.append(
            {
                "region_index": region_index + 1,
                "background_color": backgrounds.get(region_index),
                "filled_cells": filled_cells,
            }
        )

    rendered_cells: dict[tuple[int, int], str] = {}
    for row_index, row in enumerate(rows, start=1):
        lines.append("\t<tr>")
        for column_index, cell in enumerate(row, start=1):
            anchor = filled_from_anchor.get((row_index, column_index))
            if anchor is not None:
                converted = rendered_cells[anchor]
                cell_images = []
                cell_degradations = []
            else:
                converted, cell_images, cell_degradations = convert_table_cell(
                    cell, next_image_number=len(table_images) + 1
                )
            rendered_cells[(row_index, column_index)] = converted
            background = cell_backgrounds.get((row_index, column_index))
            color_attribute = f' color="{background}"' if background else ""
            lines.append(f"\t\t<td{color_attribute}>{converted}</td>")
            for image in cell_images:
                table_images.append({"row": row_index, "column": column_index, **image})
            for degradation in cell_degradations:
                degradations.append({"row": row_index, "column": column_index, **degradation})
        lines.append("\t</tr>")
    lines.append("</table>")
    enhancement = {
        "fill_merged_cells": fill_merged_cells,
        "color_merged_cells": color_merged_cells,
        "coloring_strategy": (
            "deterministic_adjacent_region_graph_coloring_lowest_available_palette"
            if color_merged_cells and table.merges
            else None
        ),
        "color_reuse_policy": (
            "reuse_only_for_non_touching_regions" if color_merged_cells and table.merges else None
        ),
        "palette_used": list(dict.fromkeys(backgrounds.values())),
        "regions": enhanced_regions,
    }
    return "\n".join(lines), degradations, table_images, enhancement


def text_with_standalone_images(
    value: str, *, protect_numbered_prefix: bool = False
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, str]]]:
    """Split Markdown inline images into ordered standalone Notion image blocks."""
    sections: list[str] = []
    images: list[dict[str, Any]] = []
    degradations: list[dict[str, str]] = []
    cursor = 0
    for match in MARKDOWN_IMAGE.finditer(value):
        before = value[cursor : match.start()]
        if before:
            converted, formula_degradations = rich_text_with_inline_formulas(
                before,
                protect_numbered_prefix=protect_numbered_prefix and cursor == 0,
                accept_standard_formulas=True,
            )
            if converted.strip():
                sections.append(converted.strip())
            degradations.extend(formula_degradations)
        sections.append(f"![{escape_rich_text(match.group(1))}]({match.group(2)})")
        images.append(
            {
                "url": match.group(2),
                "source_alt": match.group(1),
                "caption_empty": match.group(1) == "",
            }
        )
        cursor = match.end()
    after = value[cursor:]
    if after:
        converted, formula_degradations = rich_text_with_inline_formulas(
            after,
            protect_numbered_prefix=protect_numbered_prefix and cursor == 0,
            accept_standard_formulas=True,
        )
        if converted.strip():
            sections.append(converted.strip())
        degradations.extend(formula_degradations)
    return sections, images, degradations


@dataclass(frozen=True)
class Conversion:
    title: str
    content: str
    manifest: dict[str, Any]


def convert(
    source_path: Path,
    *,
    fill_merged_cells: bool = False,
    color_merged_cells: bool = False,
) -> Conversion:
    blocks = load_blocks(source_path)
    footnote_groups, consumed_footnote_texts = resolve_footnotes(blocks)
    footnote_output_orders: dict[tuple[Any, Any], int] = {}
    title_blocks = [block for block in blocks if block["type"] == "title"]
    single_title_block = title_blocks[0] if len(title_blocks) == 1 else None
    title = single_title_block["content"] if single_title_block else filename_title(source_path)
    sections: list[str] = []
    mappings: list[dict[str, Any]] = []
    output_order = 1
    native_toc_emitted = False

    if single_title_block:
        mappings.append(
            {
                "order": output_order,
                "output_order": output_order,
                "source_page_num": single_title_block["_source_page_num"],
                "source_idx": single_title_block["idx"],
                "source_type": "title",
                "target_type": "page_title_property",
                "degraded": False,
            }
        )
        output_order += 1

    for block_position, block in enumerate(blocks):
        source_type = block["type"]
        content = block.get("content", "")
        identity = source_identity(block)
        if identity in consumed_footnote_texts:
            parent_identity = consumed_footnote_texts[identity]
            parent_output_order = footnote_output_orders[parent_identity]
            mappings.append(
                {
                    "order": parent_output_order,
                    "output_order": parent_output_order,
                    "source_page_num": block["_source_page_num"],
                    "source_idx": block["idx"],
                    "source_type": source_type,
                    "target_type": "quote",
                    "degraded": False,
                    "grouped_into_footnote": {
                        "source_page_num": parent_identity[0],
                        "source_idx": parent_identity[1],
                    },
                }
            )
            continue
        if source_type in FILTERED_TYPES:
            mappings.append(
                {
                    "order": None,
                    "output_order": None,
                    "source_page_num": block["_source_page_num"],
                    "source_idx": block["idx"],
                    "source_type": source_type,
                    "target_type": "filtered",
                    "degraded": False,
                    "filter_reason": "default_non_body_element_filter",
                }
            )
            continue
        if source_type == "title":
            if single_title_block:
                continue
            converted_text, inline_degradations = rich_text_with_inline_formulas(content)
            sections.append(f"## {converted_text}")
            target_types = ["heading_2"]
        elif source_type == "text":
            text_sections, inline_images, inline_degradations = text_with_standalone_images(
                content, protect_numbered_prefix=True
            )
            sections.extend(text_sections)
            target_types = ["paragraph", "image"] if inline_images else ["paragraph"]
        elif source_type in {"figure_caption", "table_caption"}:
            converted_text, inline_degradations = rich_text_with_inline_formulas(
                content
            )
            sections.append(converted_text)
            target_types = ["paragraph"]
        elif source_type == "blank":
            mappings.append(
                {
                    "order": None,
                    "output_order": None,
                    "source_page_num": block["_source_page_num"],
                    "source_idx": block["idx"],
                    "source_type": source_type,
                    "target_type": "filtered",
                    "degraded": False,
                    "filter_reason": "blank_is_structural_fill_position_marker",
                }
            )
            continue
        elif source_type == "reference":
            if content == "":
                mappings.append(
                    {
                        "order": None,
                        "output_order": None,
                        "source_page_num": block["_source_page_num"],
                        "source_idx": block["idx"],
                        "source_type": source_type,
                        "target_type": "filtered",
                        "degraded": False,
                        "filter_reason": "empty_region_or_container_marker",
                    }
                )
                continue
            converted_text, inline_degradations = rich_text_with_inline_formulas(
                content, protect_numbered_prefix=True
            )
            sections.append(converted_text)
            target_types = ["paragraph"]
        elif source_type == "cate":
            if native_toc_emitted:
                mappings.append(
                    {
                        "order": None,
                        "output_order": None,
                        "source_page_num": block["_source_page_num"],
                        "source_idx": block["idx"],
                        "source_type": source_type,
                        "target_type": "filtered",
                        "degraded": False,
                        "filter_reason": "continued_source_toc_covered_by_native_toc",
                    }
                )
                continue
            sections.append("<table_of_contents/>")
            target_types = ["table_of_contents"]
            native_toc_emitted = True
        elif source_type == "footnote":
            footnote_group = footnote_groups[identity]
            content = footnote_group["content"]
            if content == "":
                mappings.append(
                    {
                        "order": None,
                        "output_order": None,
                        "source_page_num": block["_source_page_num"],
                        "source_idx": block["idx"],
                        "source_type": source_type,
                        "target_type": "filtered",
                        "degraded": False,
                        "filter_reason": "empty_footnote_region_without_recoverable_text",
                    }
                )
                continue
            converted_text, inline_degradations = rich_text_with_inline_formulas(
                content, protect_numbered_prefix=True, accept_standard_formulas=True
            )
            quote_lines = converted_text.splitlines() or [""]
            escaped_label = escape_rich_text(FOOTNOTE_LABEL)
            sections.append(
                "\n".join(
                    [
                        f"> {escaped_label} {quote_lines[0]}".rstrip(),
                        *(f"> {line}".rstrip() for line in quote_lines[1:]),
                    ]
                )
            )
            target_types = ["quote"]
        elif source_type == "choice":
            items = content.splitlines()
            if not items:
                raise ValueError("choice must contain at least one item")
            converted_items: list[str] = []
            choice_degradations: list[dict[str, str]] = []
            for item in items:
                converted_item, item_degradations = rich_text_with_inline_formulas(
                    item, accept_standard_formulas=True
                )
                converted_items.append(f"- {converted_item}")
                choice_degradations.extend(item_degradations)
            sections.append("\n".join(converted_items))
            target_types = ["bulleted_list_item"] * len(items)
        elif source_type == "table":
            table = parse_table(content)
            table_nfm, formula_degradations, table_images, merge_enhancement = table_to_nfm(
                table,
                fill_merged_cells=fill_merged_cells,
                color_merged_cells=color_merged_cells,
            )
            sections.append(table_nfm)
            sections.extend(
                f"![{escape_rich_text(image['label'] + ('：' + image['caption'] if image['caption'] else ''))}]({image['url']})"
                for image in table_images
            )
            target_types = ["table"] + ["table_row"] * len(table.rows) + ["image"] * len(table_images)
        elif source_type in {"figure", "stamp"}:
            image_url = block.get("img_url", "")
            if not image_url:
                raise ValueError(f"{source_type} img_url must not be empty")
            sections.append(f"![{escape_rich_text(content)}]({image_url})")
            target_types = ["image", "caption"]
        elif source_type == "cs":
            image_url = block.get("img_url", "")
            if not image_url:
                raise ValueError("cs img_url must not be empty")
            sections.append(f"![{escape_rich_text(content)}]({image_url})")
            target_types = ["image", "caption"]
        elif source_type == "code":
            source_language = block.get("code_language", "")
            language = source_language if source_language else "plain text"
            sections.append(f"```{language}\n{content}\n```")
            target_types = ["code"]
        elif source_type == "equation":
            sections.append(f"$$\n{content}\n$$")
            target_types = ["equation"]
        else:  # guarded by load_blocks
            raise AssertionError(source_type)

        mapping: dict[str, Any] = {
                "order": output_order,
                "output_order": output_order,
                "source_page_num": block["_source_page_num"],
                "source_idx": block["idx"],
                "source_type": source_type,
                "target_type": target_types[0] if len(target_types) == 1 else target_types,
                "degraded": False,
            }
        if source_type in {"title", "text", "figure_caption", "table_caption", "reference", "footnote"}:
            mapping["inline_formula"] = {
                "source_format": block.get("format", ""),
                "target_format": "nfm_inline_equation_rich_text",
                "degradations": inline_degradations,
            }
            mapping["degraded"] = bool(inline_degradations)
        if source_type == "text" and inline_images:
            mapping["inline_image_degradation"] = {
                "used": True,
                "strategy": "ordered_standalone_image_blocks",
                "reason": "notion_paragraphs_do_not_support_inline_image_blocks",
                "images": inline_images,
            }
            mapping["degraded"] = True
        if source_type == "reference":
            mapping["degraded"] = True
            mapping["degradation_reason"] = "nonempty_region_element_rendered_as_paragraph"
        elif source_type == "footnote":
            mapping["footnote"] = {
                "label": FOOTNOTE_LABEL,
                "content_origin": footnote_group["content_origin"],
                "child_source_indices": footnote_group["child_source_indices"],
                "target_format": "nfm_quote",
            }
            mapping["degraded"] = True
            mapping["degradation_reason"] = "footnote_rendered_as_prefixed_quote"
        if source_type == "table":
            mapping["header"] = {
                "enabled": table.header_row,
                "inferred": table.header_inferred,
                "rule": table.header_rule,
            }
            mapping["inline_formula_degradations"] = formula_degradations
            mapping["cell_image_degradation"] = {
                "used": bool(table_images),
                "strategy": "numbered_cell_links_then_ordered_standalone_image_blocks",
                "reason": "notion_tables_do_not_support_image_blocks_in_cells",
                "coordinate_base": 1,
                "images": [
                    {"source_idx": block["idx"], **image}
                    for image in table_images
                ],
            }
            mapping["merge_degradation"] = {
                "used": bool(table.merges),
                "strategy": (
                    "rectangular_expansion_fill_and_background_grouping"
                    if fill_merged_cells and color_merged_cells
                    else "rectangular_expansion_repeated_content"
                    if fill_merged_cells
                    else "rectangular_expansion_background_grouping_top_left_content_only"
                    if color_merged_cells
                    else "rectangular_expansion_top_left_content_only"
                ),
                "notion_ui_manual_merge_supported": True,
                "nfm_api_merge_created": False,
                "coordinate_base": 1,
                "ranges": [
                    {"source_idx": block["idx"], **merge}
                    for merge in table.merges
                ],
                "enhancement": merge_enhancement,
            }
            mapping["degraded"] = bool(formula_degradations or table.merges or table_images)
        elif source_type == "choice":
            mapping["inline_formula"] = {
                "target_format": "nfm_inline_equation_rich_text",
                "degradations": choice_degradations,
            }
            mapping["degraded"] = bool(choice_degradations)
        elif source_type == "code":
            mapping["code"] = {
                "source_format": block.get("format", ""),
                "language": language,
                "language_origin": "source_code_language" if source_language else "plain_text_fallback",
                "fallback_used": not bool(source_language),
                "content_sha256": content_digest(content),
            }
            mapping["degraded"] = not bool(source_language)
        elif source_type == "equation":
            mapping["equation"] = {
                "source_format": block.get("format", ""),
                "target_format": "nfm_equation_block",
                "content_sha256": content_digest(content),
            }
        elif source_type in {"figure", "stamp", "cs"}:
            mapping["image"] = {
                "url": image_url,
                "caption_origin": "source_content",
                "caption_empty": content == "",
                "caption_sha256": content_digest(content),
            }
        mappings.append(mapping)
        if source_type == "footnote":
            footnote_output_orders[identity] = output_order
        output_order += 1

    manifest = {
        "format": "somark-to-notion-nfm-manifest-v1",
        "source": source_path.as_posix(),
        "merge_enhancement_options": {
            "fill_merged_cells": fill_merged_cells,
            "color_merged_cells": color_merged_cells,
        },
        "page_title_origin": (
            "source_title" if single_title_block
            else "filename_fallback_multiple_titles" if len(title_blocks) > 1
            else "filename_fallback"
        ),
        "source_title": title if single_title_block else None,
        "filename_fallback": None if single_title_block else title,
        "mappings": mappings,
    }
    return Conversion(
        title=title,
        content="\n\n".join(sections) + "\n",
        manifest=manifest,
    )


def write_conversion(conversion: Conversion, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "page.nfm.md").write_text(conversion.content, encoding="utf-8", newline="\n")
    manifest_text = json.dumps(conversion.manifest, ensure_ascii=False, indent=2) + "\n"
    (output_dir / "manifest.json").write_text(manifest_text, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--fill-merged-cells", action="store_true")
    parser.add_argument("--color-merged-cells", action="store_true")
    args = parser.parse_args()
    conversion = convert(
        args.source,
        fill_merged_cells=args.fill_merged_cells,
        color_merged_cells=args.color_merged_cells,
    )
    write_conversion(conversion, args.output_dir)
    # Keep stdout ASCII-only: Windows callers may decode it using a legacy
    # console code page. JSON parsing restores the original Unicode title.
    print(json.dumps({"title": conversion.title}, ensure_ascii=True))


if __name__ == "__main__":
    main()
