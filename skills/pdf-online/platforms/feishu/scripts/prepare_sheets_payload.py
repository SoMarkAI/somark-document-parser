#!/usr/bin/env python3
"""Prepare Feishu Sheets payloads from SoMark JSON table elements."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from convert_to_feishu import SoMarkTableParser


INVALID_SHEET_NAME_RE = re.compile(r"[\\/:?*\[\]]+")
IMAGE_MARKDOWN_RE = re.compile(
    r"!\[(?P<alt>.*?)\]\(\s*(?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)",
    re.DOTALL,
)


def unwrap_somark_json(data: Any) -> dict[str, Any]:
    """Accept both raw parsed JSON and the official full API response."""
    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        return data
    if not isinstance(data, dict):
        return {}
    task_data = data.get("data") if isinstance(data.get("data"), dict) else data
    result = task_data.get("result") if isinstance(task_data, dict) else None
    outputs = result.get("outputs") if isinstance(result, dict) else None
    payload = outputs.get("json") if isinstance(outputs, dict) else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}
LATEX_MATH_RE = re.compile(
    r"\$\$(?P<block>.*?)\$\$|(?<!\$)\$(?!\$)(?P<inline>.*?)(?<!\$)\$(?!\$)",
    re.DOTALL,
)
LATEX_WRAPPER_RE = re.compile(
    r"\\(?:mathrm|mathbf|text|operatorname)\s*\{([^{}]*)\}"
)
LATEX_FRACTION_RE = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
LATEX_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]+)\}")
UNKNOWN_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
SPACE_RE = re.compile(r"[ \t]+")
LATEX_REPLACEMENTS = {
    r"\varepsilon": "ε",
    r"\vartheta": "ϑ",
    r"\varphi": "ϕ",
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\leftrightarrow": "↔",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔",
    r"\geqslant": "≥",
    r"\leqslant": "≤",
    r"\parallel": "∥",
    r"\subseteq": "⊆",
    r"\supseteq": "⊇",
    r"\notin": "∉",
    r"\partial": "∂",
    r"\nabla": "∇",
    r"\infty": "∞",
    r"\propto": "∝",
    r"\approx": "≈",
    r"\equiv": "≡",
    r"\degree": "°",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\epsilon": "ε",
    r"\zeta": "ζ",
    r"\eta": "η",
    r"\theta": "θ",
    r"\iota": "ι",
    r"\kappa": "κ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\nu": "ν",
    r"\xi": "ξ",
    r"\omicron": "ο",
    r"\pi": "π",
    r"\rho": "ρ",
    r"\sigma": "σ",
    r"\tau": "τ",
    r"\upsilon": "υ",
    r"\phi": "φ",
    r"\chi": "χ",
    r"\psi": "ψ",
    r"\omega": "ω",
    r"\Gamma": "Γ",
    r"\Delta": "Δ",
    r"\Theta": "Θ",
    r"\Lambda": "Λ",
    r"\Xi": "Ξ",
    r"\Pi": "Π",
    r"\Sigma": "Σ",
    r"\Phi": "Φ",
    r"\Psi": "Ψ",
    r"\Omega": "Ω",
    r"\pm": "±",
    r"\mp": "∓",
    r"\times": "×",
    r"\cdot": "·",
    r"\div": "÷",
    r"\sim": "~",
    r"\geq": "≥",
    r"\leq": "≤",
    r"\neq": "≠",
    r"\ne": "≠",
    r"\to": "→",
    r"\uparrow": "↑",
    r"\downarrow": "↓",
    r"\sum": "∑",
    r"\prod": "∏",
    r"\int": "∫",
    r"\angle": "∠",
    r"\perp": "⊥",
    r"\in": "∈",
    r"\subset": "⊂",
    r"\supset": "⊃",
    r"\cup": "∪",
    r"\cap": "∩",
    r"\ldots": "…",
    r"\cdots": "…",
    r"\quad": " ",
    r"\qquad": " ",
}

SUPERSCRIPT_CHARS = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "n": "ⁿ",
    "i": "ⁱ",
}

SUBSCRIPT_CHARS = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "x": "ₓ",
}


@dataclass
class PlacedCell:
    row: int
    column: int
    rowspan: int
    colspan: int
    text: str
    is_header: bool
    image_urls: list[str] = field(default_factory=list)


def column_name(index: int) -> str:
    value = index + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def raw_text(value: str) -> str:
    text = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")

    # Sheets are layout-restoration outputs whose cells are plain text. Keep
    # image URLs readable instead of asking Feishu Sheets to parse Markdown
    # image syntax as a second, nested link.
    def flatten_image(match: re.Match[str]) -> str:
        alt = SPACE_RE.sub(" ", match.group("alt")).strip()
        url = match.group("url").strip()
        return f"{alt} {url}".strip() if alt else url

    text = IMAGE_MARKDOWN_RE.sub(flatten_image, text)
    return "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.split("\n")).strip()


def extract_image_urls(value: str) -> list[str]:
    return [match.group("url") for match in IMAGE_MARKDOWN_RE.finditer(value)]


def _script_text(value: str, mapping: dict[str, str], marker: str) -> str:
    compact = SPACE_RE.sub("", value)
    if compact and all(char in mapping for char in compact):
        return "".join(mapping[char] for char in compact)
    return marker + (compact if len(compact) == 1 else f"({compact})")


def _simple_fraction(match: re.Match[str]) -> str:
    numerator, denominator = match.groups()
    if re.fullmatch(r"[A-Za-z0-9.+-]+", numerator) and re.fullmatch(
        r"[A-Za-z0-9.+-]+", denominator
    ):
        return f"{numerator}/{denominator}"
    return f"({numerator})/({denominator})"


def _latex_body_to_unicode(value: str) -> str:
    text = value
    text = re.sub(r"\^\s*(?:\{\s*\\circ\s*\}|\\circ)", "°", text)
    previous = None
    while previous != text:
        previous = text
        text = LATEX_WRAPPER_RE.sub(r"\1", text)
        text = LATEX_FRACTION_RE.sub(_simple_fraction, text)
        text = LATEX_SQRT_RE.sub(r"√(\1)", text)
    for source, target in sorted(
        LATEX_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = re.sub(re.escape(source) + r"(?![A-Za-z])", lambda _: target, text)
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = re.sub(
        r"\^\s*\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), SUPERSCRIPT_CHARS, "^"),
        text,
    )
    text = re.sub(
        r"\^\s*([0-9+\-=()ni])",
        lambda match: _script_text(match.group(1), SUPERSCRIPT_CHARS, "^"),
        text,
    )
    text = re.sub(
        r"_\s*\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), SUBSCRIPT_CHARS, "_"),
        text,
    )
    text = re.sub(
        r"_\s*([0-9+\-=()aeijklm?noprstxh])",
        lambda match: _script_text(match.group(1), SUBSCRIPT_CHARS, "_"),
        text,
    )
    for spacing in (r"\,", r"\;", r"\:", r"\!", r"\ "):
        text = text.replace(spacing, " ")
    text = text.replace(r"\%", "%").replace(r"\&", "&")
    return SPACE_RE.sub(" ", text).strip()


def readable_text(value: str) -> str:
    text = raw_text(value)

    def convert_math(match: re.Match[str]) -> str:
        body = match.group("block")
        if body is None:
            body = match.group("inline") or ""
        converted = _latex_body_to_unicode(body)
        if UNKNOWN_LATEX_COMMAND_RE.search(converted):
            return match.group(0)
        return converted

    text = LATEX_MATH_RE.sub(convert_math, text)
    return "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.split("\n")).strip()


def parse_table(
    source: str, *, text_mode: str = "readable"
) -> tuple[list[list[str | None]], list[dict[str, str]], dict[str, Any]]:
    parser = SoMarkTableParser()
    parser.feed(source)
    parser.close()
    if not parser.rows or not any(parser.rows):
        raise ValueError("HTML table contained no rows or cells")

    transform = raw_text if text_mode == "raw" else readable_text
    occupied: set[tuple[int, int]] = set()
    placements: list[PlacedCell] = []
    row_count = len(parser.rows)
    column_count = 0

    for row_index, row in enumerate(parser.rows):
        column_index = 0
        for cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            placements.append(
                PlacedCell(
                    row=row_index,
                    column=column_index,
                    rowspan=cell.rowspan,
                    colspan=cell.colspan,
                    text=transform(cell.text),
                    is_header=cell.is_header,
                    image_urls=list(cell.image_urls) + extract_image_urls(cell.text),
                )
            )
            for target_row in range(row_index, row_index + cell.rowspan):
                for target_column in range(column_index, column_index + cell.colspan):
                    occupied.add((target_row, target_column))
            row_count = max(row_count, row_index + cell.rowspan)
            column_count = max(column_count, column_index + cell.colspan)
            column_index += cell.colspan

    grid: list[list[str | None]] = [
        [None for _ in range(column_count)] for _ in range(row_count)
    ]
    merges: list[dict[str, str]] = []
    formula_like_cells = 0
    for cell in placements:
        grid[cell.row][cell.column] = cell.text
        if "\\" in cell.text or "^" in cell.text or "_" in cell.text:
            formula_like_cells += 1
        if cell.rowspan > 1 or cell.colspan > 1:
            start = f"{column_name(cell.column)}{cell.row + 1}"
            end = f"{column_name(cell.column + cell.colspan - 1)}{cell.row + cell.rowspan}"
            merges.append({"range": f"{start}:{end}", "merge_type": "all"})

    images: list[dict[str, Any]] = []
    for cell in placements:
        urls: list[str] = []
        for url in cell.image_urls:
            if url and url not in urls:
                urls.append(url)
        if not urls:
            continue
        images.append(
            {
                "cell": f"{column_name(cell.column)}{cell.row + 1}",
                "row": cell.row,
                "column": cell.column,
                "urls": urls,
            }
        )

    return grid, merges, {
        "rows": row_count,
        "columns": column_count,
        "merged_cells": len(merges),
        "formula_like_cells": formula_like_cells,
        "images": images,
    }


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def estimate_column_widths(grid: list[list[str | None]]) -> list[int]:
    widths: list[int] = []
    for column in range(len(grid[0])):
        content_width = 0
        for row in grid:
            value = row[column] or ""
            for line in value.split("\n"):
                content_width = max(content_width, display_width(line))
        widths.append(max(80, min(260, content_width * 8 + 20)))
    return widths


def safe_sheet_name(
    title: str, index: int, used: set[str], *, prefix: str | None = None
) -> str:
    title = INVALID_SHEET_NAME_RE.sub(" ", title)
    title = SPACE_RE.sub(" ", title).strip(" '!")
    sheet_prefix = prefix if prefix is not None else f"{index:02d}_"
    base = (sheet_prefix + (title or "未命名表格"))[:60].rstrip()
    candidate = base
    suffix = 2
    while candidate in used:
        marker = f"_{suffix}"
        candidate = (base[: 60 - len(marker)] + marker).rstrip()
        suffix += 1
    used.add(candidate)
    return candidate


def normalize_asset_reference(value: str) -> str:
    path = unquote(urlparse(value.strip()).path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/").casefold()


def build_asset_index(asset_dir: Path | None) -> dict[str, str]:
    if asset_dir is None or not asset_dir.exists():
        return {}
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".heic"}
    index: dict[str, str] = {}
    basenames: dict[str, list[str]] = {}
    for path in asset_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        resolved = str(path.resolve())
        relative_key = normalize_asset_reference(path.relative_to(asset_dir).as_posix())
        index[relative_key] = resolved
        basenames.setdefault(path.name.casefold(), []).append(resolved)
    for name, matches in basenames.items():
        if len(matches) == 1:
            index.setdefault(name, matches[0])
    return index


def local_asset_for_url(url: str, assets: dict[str, str]) -> str | None:
    key = normalize_asset_reference(url)
    if not key:
        return None
    if key in assets:
        return assets[key]
    name = Path(key).name.casefold()
    if name in assets:
        return assets[name]
    suffix = "/" + key
    matches = {
        path
        for asset_key, path in assets.items()
        if "/" in asset_key and asset_key.endswith(suffix)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def build_layout_sheet(
    name: str,
    grid: list[list[str | None]],
    merges: list[dict[str, str]],
    metrics: dict[str, int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    columns = [f"C{column + 1:02d}" for column in range(metrics["columns"])]
    last_cell = f"{column_name(metrics['columns'] - 1)}{metrics['rows']}"
    return (
        {
            "name": name,
            "start_cell": "A1",
            "mode": "overwrite",
            "header": False,
            "allow_overwrite": True,
            "columns": columns,
            "data": grid,
            "dtypes": {column: "object" for column in columns},
        },
        build_sheet_style(name, grid, last_cell, merges),
    )


def build_sheet_style(
    name: str,
    grid: list[list[str | None]],
    last_cell: str,
    merges: list[dict[str, str]],
) -> dict[str, Any]:
    border = {
        side: {"style": "solid", "color": "#D0D5DD", "weight": "thin"}
        for side in ("top", "bottom", "left", "right")
    }
    return {
        "name": name,
        "cell_styles": [
            {
                "range": f"A1:{last_cell}",
                "vertical_alignment": "middle",
                "word_wrap": "auto-wrap",
                "border_styles": border,
            }
        ],
        "row_sizes": [{"range": f"1:{len(grid)}", "type": "auto"}],
        "col_sizes": [
            {
                "range": f"{column_name(index)}:{column_name(index)}",
                "type": "pixel",
                "size": width,
            }
            for index, width in enumerate(estimate_column_widths(grid))
        ],
        "cell_merges": merges,
    }


def build_payload(
    source: Path,
    *,
    text_mode: str = "readable",
    max_tables: int | None = None,
    asset_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    document = unwrap_somark_json(json.loads(source.read_text(encoding="utf-8-sig")))
    used_names: set[str] = set()
    sheets: list[dict[str, Any]] = []
    styles: list[dict[str, Any]] = []
    manifest_tables: list[dict[str, Any]] = []
    assets = build_asset_index(asset_dir)
    table_index = 0

    for page in document.get("pages", []):
        title_context = ""
        caption_context = ""
        for block in page.get("blocks", []):
            block_type = block.get("type")
            if block_type == "title":
                title_context = readable_text(str(block.get("content") or ""))
                continue
            elif block_type == "table_caption":
                caption_context = readable_text(str(block.get("content") or ""))
                continue
            elif block_type != "table":
                continue

            table_index += 1
            source_title = caption_context or title_context or f"第{page.get('page_num', 0) + 1}页表格"
            grid, merges, metrics = parse_table(
                str(block.get("content") or ""), text_mode=text_mode
            )
            for image in metrics.get("images", []):
                image["local_path"] = next(
                    (
                        local_asset_for_url(url, assets)
                        for url in image["urls"]
                        if local_asset_for_url(url, assets)
                    ),
                    None,
                )
                row = image["row"]
                column = image["column"]
                if (
                    not image["local_path"]
                    and row < len(grid)
                    and column < len(grid[row])
                    and not (grid[row][column] or "").strip()
                ):
                    grid[row][column] = "\n".join(image["urls"])
                if image["local_path"]:
                    if row < len(grid) and column < len(grid[row]):
                        cell_value = grid[row][column] or ""
                        image_references = {
                            html.unescape(url).strip() for url in image["urls"]
                        }
                        is_bare_image_reference = (
                            cell_value.strip() in image_references
                            and local_asset_for_url(cell_value.strip(), assets)
                        )
                        if (
                            not cell_value
                            or IMAGE_MARKDOWN_RE.fullmatch(cell_value)
                            or is_bare_image_reference
                        ):
                            grid[row][column] = None
            sheet_name = safe_sheet_name(source_title, table_index, used_names)
            sheet, style = build_layout_sheet(sheet_name, grid, merges, metrics)
            sheets.append(sheet)
            styles.append(style)

            manifest_tables.append(
                {
                    "index": table_index,
                    "page": int(page.get("page_num", 0)) + 1,
                    "block_index": block.get("idx"),
                    "source_title": source_title,
                    "sheet_name": sheet_name,
                    **metrics,
                }
            )
            caption_context = ""
            if max_tables is not None and table_index >= max_tables:
                break
        if max_tables is not None and table_index >= max_tables:
            break

    if not sheets:
        raise ValueError("SoMark JSON contained no table blocks")

    manifest = {
        "source": str(source.resolve()),
        "text_mode": text_mode,
        "mode": "layout",
        "cell_value_type": "text",
        "table_count": len(manifest_tables),
        "output_sheet_count": len(sheets),
        "total_cells": sum(item["rows"] * item["columns"] for item in manifest_tables),
        "total_merges": sum(item["merged_cells"] for item in manifest_tables),
        "tables": manifest_tables,
        "asset_dir": str(asset_dir.resolve()) if asset_dir else None,
        "local_image_count": sum(
            1
            for table in manifest_tables
            for image in table.get("images", [])
            if image.get("local_path")
        ),
    }
    return {"sheets": sheets}, {"styles": styles}, manifest


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--text-mode", choices=("readable", "raw"), default="readable"
    )
    parser.add_argument("--max-tables", type=int)
    parser.add_argument(
        "--asset-dir",
        type=Path,
        help=(
            "可选的已有本地图片目录；省略时，增强阶段会按 SoMark JSON "
            "表格单元格中的 HTTP(S) 图片 URL 下载"
        ),
    )
    args = parser.parse_args()

    sheets, styles, manifest = build_payload(
        args.source,
        text_mode=args.text_mode,
        max_tables=args.max_tables,
        asset_dir=args.asset_dir,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "sheets_payload.json").write_text(
        json.dumps(sheets, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "styles_payload.json").write_text(
        json.dumps(styles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
