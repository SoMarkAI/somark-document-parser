"""Faithful, non-cleaning reconstruction of SoMark HTML table blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .sheet_models import (
    DimensionPlan,
    ImagePlan,
    MergePlan,
    SourceRowMapping,
    StylePlan,
    WorksheetPlan,
)


_SPACE_RE = re.compile(r"[ \t]+")
_LATEX_MATH_RE = re.compile(
    r"\$\$(?P<block>.*?)\$\$|(?<!\$)\$(?!\$)(?P<inline>.*?)(?<!\$)\$(?!\$)",
    re.DOTALL,
)
_LATEX_WRAPPER_RE = re.compile(r"\\(?:mathrm|mathbf|text|operatorname)\s*\{([^{}]*)\}")
_LATEX_FRACTION_RE = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
_LATEX_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]+)\}")
_UNKNOWN_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INVALID_SHEET_NAME_RE = re.compile(r"[\\/:?*\[\]]+")
_A1_RANGE_RE = re.compile(r"^([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)$")
_MAX_SHEET_NAME_LENGTH = 30

_LATEX_REPLACEMENTS = {
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

_SUPERSCRIPT_CHARS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}

_SUBSCRIPT_CHARS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ",
    "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ",
    "s": "ₛ", "t": "ₜ", "x": "ₓ",
}


def column_name(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    value = index + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def a1_range(start_row: int, end_row: int, columns: int) -> str:
    if start_row < 1 or end_row < start_row or columns < 1:
        raise ValueError("invalid A1 range dimensions")
    return f"A{start_row}:{column_name(columns - 1)}{end_row}"


def _integer(value: Any, default: int = 1) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _css_map(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in (value or "").split(";"):
        if ":" not in declaration:
            continue
        key, item = declaration.split(":", 1)
        key = key.strip().casefold()
        item = item.strip()
        if key and item:
            result[key] = item
    return result


def _hex_color(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"#[0-9a-fA-F]{6}\b", value)
    if match:
        return match.group(0).upper()
    short = re.search(r"#[0-9a-fA-F]{3}\b", value)
    if short:
        chars = short.group(0)[1:]
        return "#" + "".join(char * 2 for char in chars).upper()
    return None


def _pixel(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(px|pt)?", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    if number <= 0:
        return None
    if (match.group(2) or "").casefold() == "pt":
        number *= 4 / 3
    return max(1, round(number))


def _normalize_text(parts: Iterable[str]) -> str:
    text = unescape("".join(parts)).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _script_text(value: str, mapping: dict[str, str], marker: str) -> str:
    compact = _SPACE_RE.sub("", value)
    if compact and all(character in mapping for character in compact):
        return "".join(mapping[character] for character in compact)
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
        text = _LATEX_WRAPPER_RE.sub(r"\1", text)
        text = _LATEX_FRACTION_RE.sub(_simple_fraction, text)
        text = _LATEX_SQRT_RE.sub(r"√(\1)", text)
    for source, target in sorted(
        _LATEX_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = re.sub(re.escape(source) + r"(?![A-Za-z])", lambda _: target, text)
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = re.sub(
        r"\^\s*\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), _SUPERSCRIPT_CHARS, "^"),
        text,
    )
    text = re.sub(
        r"\^\s*([0-9+\-=()ni])",
        lambda match: _script_text(match.group(1), _SUPERSCRIPT_CHARS, "^"),
        text,
    )
    text = re.sub(
        r"_\s*\{([^{}]+)\}",
        lambda match: _script_text(match.group(1), _SUBSCRIPT_CHARS, "_"),
        text,
    )
    text = re.sub(
        r"_\s*([0-9+\-=()aeijklm?noprstxh])",
        lambda match: _script_text(match.group(1), _SUBSCRIPT_CHARS, "_"),
        text,
    )
    for spacing in (r"\,", r"\;", r"\:", r"\!", r"\ "):
        text = text.replace(spacing, " ")
    text = text.replace(r"\%", "%").replace(r"\&", "&")
    return _SPACE_RE.sub(" ", text).strip()


def readable_latex_text(value: str) -> tuple[str, int, int]:
    """Convert only safe LaTeX fragments and preserve every complex fragment verbatim."""

    converted_count = 0
    preserved_count = 0

    def convert_math(match: re.Match[str]) -> str:
        nonlocal converted_count, preserved_count
        body = match.group("block")
        if body is None:
            body = match.group("inline") or ""
        converted = _latex_body_to_unicode(body)
        if _UNKNOWN_LATEX_COMMAND_RE.search(converted):
            preserved_count += 1
            return match.group(0)
        converted_count += 1
        return converted

    text = _LATEX_MATH_RE.sub(convert_math, value)
    text = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")).strip()
    return text, converted_count, preserved_count


@dataclass
class _Fragment:
    text: str
    style: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Cell:
    rowspan: int
    colspan: int
    is_header: bool
    attributes: dict[str, str]
    text_parts: list[str] = field(default_factory=list)
    fragments: list[_Fragment] = field(default_factory=list)
    image_references: list[tuple[str, int | None, int | None]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _normalize_text(self.text_parts)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._formats: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if name == "tr":
            self._row = []
            return
        if name in {"td", "th"}:
            if self._row is None:
                self._row = []
            self._cell = _Cell(
                rowspan=_integer(attributes.get("rowspan")),
                colspan=_integer(attributes.get("colspan")),
                is_header=name == "th",
                attributes=attributes,
            )
            return
        if self._cell is None:
            return
        if name == "br":
            self._cell.text_parts.append("\n")
            self._cell.fragments.append(_Fragment("\n"))
            return
        if name == "img":
            source = attributes.get("src") or attributes.get("data-src")
            if source:
                self._cell.image_references.append(
                    (source, _pixel(attributes.get("width")), _pixel(attributes.get("height")))
                )
            alt = attributes.get("alt")
            if alt:
                self._append_text(alt)
            return
        inherited = dict(self._formats[-1]) if self._formats else {}
        if name in {"strong", "b"}:
            inherited["bold"] = True
        elif name in {"em", "i"}:
            inherited["italic"] = True
        elif name == "u":
            inherited["underline"] = True
        elif name in {"s", "strike", "del"}:
            inherited["strike"] = True
        css = _css_map(attributes.get("style"))
        color = _hex_color(css.get("color") or attributes.get("color"))
        if color:
            inherited["color"] = color
        size = _pixel(css.get("font-size") or attributes.get("size"))
        if size:
            inherited["size"] = size
        self._formats.append(inherited)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in {"td", "th"}:
            if self._cell is not None and self._row is not None:
                self._row.append(self._cell)
            self._cell = None
            self._formats.clear()
            return
        if name == "tr":
            if self._row is not None:
                self.rows.append(self._row)
            self._row = None
            return
        if self._cell is not None and self._formats:
            self._formats.pop()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._append_text(data)

    def _append_text(self, value: str) -> None:
        if self._cell is None:
            return
        self._cell.text_parts.append(value)
        style = dict(self._formats[-1]) if self._formats else {}
        self._cell.fragments.append(_Fragment(value, style))


@dataclass
class _PlacedCell:
    row: int
    column: int
    source: _Cell
    display_text: str
    latex_converted_count: int
    latex_preserved_count: int


@dataclass
class _FragmentTable:
    page: int
    block: int
    rows: list[list[str]]
    placed: list[_PlacedCell]
    row_count: int
    column_count: int
    label: str
    begins_new_structure: bool


def _parse_table(content: str, *, page: int, block: int, label: str, begins: bool) -> _FragmentTable:
    parser = _TableParser()
    parser.feed(content)
    parser.close()
    if not parser.rows or not any(parser.rows):
        raise ValueError(f"SoMark table block page={page} block={block} contained no cells")

    occupied: set[tuple[int, int]] = set()
    placed: list[_PlacedCell] = []
    row_count = len(parser.rows)
    column_count = 0
    for row_index, source_row in enumerate(parser.rows):
        column_index = 0
        for cell in source_row:
            while (row_index, column_index) in occupied:
                column_index += 1
            source_text = cell.text
            display_text, converted_count, preserved_count = readable_latex_text(source_text)
            display_text = _MARKDOWN_IMAGE_RE.sub(
                lambda match: match.group(1), display_text
            )
            markdown_references = [
                match.group(1) for match in _MARKDOWN_IMAGE_RE.finditer(source_text)
            ]
            if not display_text and (cell.image_references or markdown_references):
                display_text = "\n".join(
                    reference for reference, _width, _height in cell.image_references
                ) or "\n".join(markdown_references)
            placed.append(
                _PlacedCell(
                    row_index,
                    column_index,
                    cell,
                    display_text,
                    converted_count,
                    preserved_count,
                )
            )
            for target_row in range(row_index, row_index + cell.rowspan):
                for target_column in range(column_index, column_index + cell.colspan):
                    occupied.add((target_row, target_column))
            row_count = max(row_count, row_index + cell.rowspan)
            column_count = max(column_count, column_index + cell.colspan)
            column_index += cell.colspan
    if column_count < 1:
        raise ValueError(f"SoMark table block page={page} block={block} has no target columns")

    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in placed:
        grid[cell.row][cell.column] = cell.display_text
    return _FragmentTable(page, block, grid, placed, row_count, column_count, label, begins)


def unwrap_somark_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("pages"), list):
        return value
    if not isinstance(value, dict):
        return {}
    data = value.get("data") if isinstance(value.get("data"), dict) else value
    result = data.get("result") if isinstance(data, dict) else None
    outputs = result.get("outputs") if isinstance(result, dict) else None
    payload = outputs.get("json") if isinstance(outputs, dict) else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _safe_sheet_name(label: str, index: int, used: set[str]) -> str:
    cleaned = _INVALID_SHEET_NAME_RE.sub(" ", unescape(label or ""))
    cleaned = _SPACE_RE.sub(" ", cleaned).strip(" '!")
    base = (cleaned or f"SoMark表格{index}")[:_MAX_SHEET_NAME_LENGTH].rstrip()
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        marker = f"_{suffix}"
        candidate = (
            base[: _MAX_SHEET_NAME_LENGTH - len(marker)] + marker
        ).rstrip()
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _column_index(name: str) -> int:
    value = 0
    for character in name:
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _merge_rectangle(value: str) -> tuple[int, int, int, int] | None:
    match = _A1_RANGE_RE.fullmatch(value.strip().upper())
    if match is None:
        return None
    start_column = _column_index(match.group(1))
    start_row = int(match.group(2)) - 1
    end_column = _column_index(match.group(3))
    end_row = int(match.group(4)) - 1
    if end_row < start_row or end_column < start_column:
        return None
    return start_row, start_column, end_row, end_column


def _ranges_overlap(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return not (
        left[2] < right[0]
        or right[2] < left[0]
        or left[3] < right[1]
        or right[3] < left[1]
    )


def _filter_safe_merges(
    merges: list[MergePlan], rows: list[list[str]]
) -> tuple[list[MergePlan], list[dict[str, Any]]]:
    accepted: list[MergePlan] = []
    rectangles: list[tuple[int, int, int, int]] = []
    skipped: list[dict[str, Any]] = []
    for merge in merges:
        rectangle = _merge_rectangle(merge.range)
        reason: str | None = None
        if rectangle is None:
            reason = "invalid_merge_range"
        elif any(_ranges_overlap(rectangle, previous) for previous in rectangles):
            reason = "overlaps_an_earlier_merge"
        else:
            start_row, start_column, end_row, end_column = rectangle
            for row_index in range(start_row, end_row + 1):
                for column_index in range(start_column, end_column + 1):
                    if row_index == start_row and column_index == start_column:
                        continue
                    value = (
                        rows[row_index][column_index]
                        if row_index < len(rows) and column_index < len(rows[row_index])
                        else ""
                    )
                    if value.strip():
                        reason = "would_discard_non_anchor_value"
                        break
                if reason is not None:
                    break
        if reason is not None:
            skipped.append(
                {
                    "range": merge.range,
                    "reason": reason,
                    "source_page": merge.source_page,
                    "source_block": merge.source_block,
                }
            )
            continue
        accepted.append(merge)
        rectangles.append(rectangle)
    return accepted, skipped


def _asset_key(value: str) -> str:
    path = unquote(urlparse(value.strip()).path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/").casefold()


def is_remote_image_reference(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def build_asset_index(asset_dir: str | None) -> dict[str, str]:
    if not asset_dir:
        return {}
    directory = Path(asset_dir)
    if not directory.is_dir():
        return {}
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
    index: dict[str, str] = {}
    basenames: dict[str, list[str]] = {}
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in extensions:
            continue
        resolved = str(path.resolve())
        index[_asset_key(path.relative_to(directory).as_posix())] = resolved
        basenames.setdefault(path.name.casefold(), []).append(resolved)
    for name, values in basenames.items():
        if len(values) == 1:
            index.setdefault(name, values[0])
    return index


def _local_asset(reference: str, assets: dict[str, str]) -> str | None:
    key = _asset_key(reference)
    if key in assets:
        return assets[key]
    return assets.get(Path(key).name.casefold())


def _cell_style(cell: _Cell) -> tuple[dict[str, Any], int | None, int | None]:
    css = _css_map(cell.attributes.get("style"))
    result: dict[str, Any] = {}
    background = _hex_color(css.get("background-color") or css.get("background"))
    font_color = _hex_color(css.get("color"))
    if background:
        result["bg-color"] = background
    if font_color:
        result["font-color"] = font_color
    weight = css.get("font-weight", "").casefold()
    if cell.is_header or weight in {"bold", "bolder", "600", "700", "800", "900"}:
        result["font-weight"] = "bold"
    align = (css.get("text-align") or cell.attributes.get("align") or "").casefold()
    if align in {"left", "center", "right", "general"}:
        result["h-align"] = align
    vertical = (css.get("vertical-align") or cell.attributes.get("valign") or "").casefold()
    if vertical in {"top", "middle", "bottom"}:
        result["v-align"] = vertical
    white_space = css.get("white-space", "").casefold()
    if white_space in {"normal", "pre-wrap", "break-spaces"}:
        result["word-wrap"] = "autoWrap"
    size = _pixel(css.get("font-size"))
    if size:
        result["font-size"] = size
    visible_fragments = [fragment for fragment in cell.fragments if fragment.text.strip()]
    if visible_fragments:
        inline_keys = {
            "bold": ("font-weight", "bold"),
            "italic": ("font-style", "italic"),
            "underline": ("text-underline", True),
            "strike": ("text-line-through", True),
            "color": ("font-color", None),
            "size": ("font-size", None),
        }
        for fragment_key, (style_key, fixed_value) in inline_keys.items():
            values = [fragment.style.get(fragment_key) for fragment in visible_fragments]
            if values[0] is not None and all(value == values[0] for value in values):
                result.setdefault(style_key, fixed_value if fixed_value is not None else values[0])
    width = _pixel(cell.attributes.get("width") or css.get("width"))
    height = _pixel(cell.attributes.get("height") or css.get("height"))
    return result, width, height


def _fragments_from_document(document: dict[str, Any]) -> list[_FragmentTable]:
    fragments: list[_FragmentTable] = []
    pending_label = ""
    begins_new_structure = True
    for fallback_page, page in enumerate(document.get("pages") or []):
        if not isinstance(page, dict):
            continue
        raw_page = page.get("page_num", fallback_page)
        try:
            page_number = int(raw_page) + 1
        except (TypeError, ValueError):
            page_number = fallback_page + 1
        for fallback_block, block in enumerate(page.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").casefold()
            if block_type in {"title", "table_caption"}:
                candidate = _normalize_text([str(block.get("content") or "")])
                if candidate:
                    pending_label = candidate
                begins_new_structure = True
                continue
            if block_type != "table":
                continue
            raw_block = block.get("idx", fallback_block)
            try:
                block_number = int(raw_block)
            except (TypeError, ValueError):
                block_number = fallback_block
            fragments.append(
                _parse_table(
                    str(block.get("content") or ""),
                    page=page_number,
                    block=block_number,
                    label=pending_label,
                    begins=begins_new_structure,
                )
            )
            begins_new_structure = False
    return fragments


def reconstruct_worksheets(document: dict[str, Any], *, assets_dir: str | None = None) -> list[WorksheetPlan]:
    """Rebuild tables without deleting, deduplicating or rewriting any source row."""

    fragments = _fragments_from_document(document)
    if not fragments:
        raise ValueError("SoMark JSON contained no table blocks")
    groups: list[list[_FragmentTable]] = [[fragment] for fragment in fragments]

    assets = build_asset_index(assets_dir)
    used_names: set[str] = set()
    worksheets: list[WorksheetPlan] = []
    for index, group in enumerate(groups, start=1):
        columns = max(fragment.column_count for fragment in group)
        rows: list[list[str]] = []
        mappings: list[SourceRowMapping] = []
        source_blocks: list[dict[str, int]] = []
        merges: list[MergePlan] = []
        styles: list[StylePlan] = []
        images: list[ImagePlan] = []
        latex_unicode_cells: list[str] = []
        latex_source_cells: list[str] = []
        literal_formula_cells: list[str] = []
        column_widths: dict[int, int] = {}
        row_heights: dict[int, int] = {}
        duplicate_headers: list[int] = []
        empty_rows: list[int] = []
        first_header: list[str] | None = None
        target_offset = 0
        for fragment_index, fragment in enumerate(group):
            source_blocks.append({"page": fragment.page, "block": fragment.block})
            padded = [row + [""] * (columns - len(row)) for row in fragment.rows]
            if padded:
                if first_header is None:
                    first_header = list(padded[0])
                elif padded[0] == first_header:
                    duplicate_headers.append(target_offset + 1)
            for local_row, row in enumerate(padded, start=1):
                target_row = target_offset + local_row
                rows.append(row)
                mappings.append(SourceRowMapping(fragment.page, fragment.block, local_row, target_row))
                if not any(value != "" for value in row):
                    empty_rows.append(target_row)
            for placed in fragment.placed:
                source = placed.source
                target_row = target_offset + placed.row + 1
                target_column = placed.column
                cell = f"{column_name(target_column)}{target_row}"
                if source.rowspan > 1 or source.colspan > 1:
                    end = f"{column_name(target_column + source.colspan - 1)}{target_row + source.rowspan - 1}"
                    merges.append(MergePlan(f"{cell}:{end}", cell, fragment.page, fragment.block))
                style, width, height = _cell_style(source)
                if style:
                    styles.append(StylePlan(cell, style))
                if width and target_column not in column_widths:
                    column_widths[target_column] = width
                if height and target_row not in row_heights:
                    row_heights[target_row] = height
                text = placed.display_text
                if placed.latex_converted_count:
                    latex_unicode_cells.append(cell)
                if placed.latex_preserved_count:
                    latex_source_cells.append(cell)
                if text.startswith("=") and len(text) > 1:
                    literal_formula_cells.append(cell)
                references = list(source.image_references)
                references.extend(
                    (match.group(1), None, None)
                    for match in _MARKDOWN_IMAGE_RE.finditer(source.text)
                )
                seen_refs: set[str] = set()
                for reference, width_px, height_px in references:
                    if reference in seen_refs:
                        continue
                    seen_refs.add(reference)
                    images.append(
                        ImagePlan(
                            cell=cell,
                            source_reference=reference,
                            local_path=_local_asset(reference, assets),
                            width=width_px,
                            height=height_px,
                        )
                    )
            target_offset += fragment.row_count

        dimensions = [
            DimensionPlan("COLUMNS", column_name(column), 1, width)
            for column, width in sorted(column_widths.items())
        ] + [
            DimensionPlan("ROWS", str(row), 1, height)
            for row, height in sorted(row_heights.items())
        ]
        label = next((fragment.label for fragment in group if fragment.label), "")
        merges, merge_degradations = _filter_safe_merges(merges, rows)
        worksheets.append(
            WorksheetPlan(
                index=index,
                name=_safe_sheet_name(label, index, used_names),
                source_title=label,
                rows=rows,
                source_blocks=source_blocks,
                row_mappings=mappings,
                styles=styles,
                merges=merges,
                dimensions=dimensions,
                images=images,
                duplicate_header_rows=duplicate_headers,
                fully_empty_rows=empty_rows,
                latex_unicode_cells=latex_unicode_cells,
                latex_source_cells=latex_source_cells,
                literal_formula_cells=literal_formula_cells,
                merge_degradations=merge_degradations,
            )
        )
    return worksheets


def load_and_reconstruct(json_path: str, *, assets_dir: str | None = None) -> list[WorksheetPlan]:
    path = Path(json_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SoMark JSON artifact does not exist: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    document = unwrap_somark_json(value)
    if not isinstance(document.get("pages"), list):
        raise ValueError("SoMark JSON did not contain a decodable pages array")
    return reconstruct_worksheets(document, assets_dir=assets_dir)


__all__ = [
    "a1_range",
    "build_asset_index",
    "column_name",
    "is_remote_image_reference",
    "load_and_reconstruct",
    "readable_latex_text",
    "reconstruct_worksheets",
    "unwrap_somark_json",
]
