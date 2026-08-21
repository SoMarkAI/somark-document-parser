"""DingTalk document route; the old implementation intentionally absent marker is kept for Foundation audit.

This module is deliberately self-contained inside the document ownership
boundary. It consumes an explicit Markdown-and-JSON pair from the current
SoMark parse or from user-specified inputs, creates a deterministic Markdown
compatibility draft, and optionally creates one new DingTalk document.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
from time import monotonic, sleep
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from .artifacts import RouteName, RouteResult, RouteTarget, SourceArtifacts
from .dws_runner import DwsRunResult, DwsRunner
from .errors import ErrorKind, StructuredError, redact_sensitive
from .manifest import ManifestStage, new_manifest, set_stage, write_manifest_atomic


DWS_CONTRACT_VERSION = "1.0.57"
COMPATIBILITY_FILENAME = "dingtalk_compatible.md"
MANIFEST_FILENAME = "document_route_manifest.json"
SUMMARY_FILENAME = "document_conversion_summary.json"
MARKDOWN_READBACK_FILENAME = "readback_markdown_summary.json"
JSONML_READBACK_FILENAME = "readback_jsonml_summary.json"
BLOCK_READBACK_FILENAME = "readback_blocks_summary.json"
PLANNED_CHUNK_CHARACTERS = 7_000
DWS_BLOCK_UPDATE_HTTP_TIMEOUT_SECONDS = 90

LEGACY_ELEMENT_TYPES_21: tuple[str, ...] = (
    "title",
    "text",
    "figure",
    "figure_caption",
    "table",
    "table_caption",
    "equation",
    "header",
    "footer",
    "sider",
    "footnote",
    "cate",
    "cate_item",
    "choice",
    "code",
    "blank",
    "reference",
    "qrcode",
    "stamp",
    "chemical_structure",
    "chemical_equation",
)

CURRENT_ELEMENT_TYPES_19: tuple[str, ...] = (
    "title",
    "text",
    "figure",
    "figure_caption",
    "table",
    "table_caption",
    "equation",
    "header",
    "footer",
    "sider",
    "footnote",
    "cate",
    "choice",
    "code",
    "blank",
    "reference",
    "qrcode",
    "stamp",
    "chemical_formula",
)

_CHEMICAL_ELEMENT_TYPES: tuple[str, ...] = (
    "chemical_formula",
    "cs",
    "cs_equation",
    "chemical_structure",
    "chemical_equation",
)

_IMAGE_RE = re.compile(
    r"!\[(?P<alt>.*?)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)",
    re.DOTALL,
)
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL_RE = re.compile(
    r"(?P<open><t[dh]\b[^>]*>)(?P<body>.*?)(?P<close></t[dh]\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_CITATION_SUPERSCRIPT_RE = re.compile(
    r"\$?\^\{(?P<body>\[[^\]\r\n]+\])\}\$?"
)
_TABLE_NOTE_SUPERSCRIPT_RE = re.compile(
    r"\$(?:\{\})?\^\{(?P<label>\d{1,3})\}\$"
)
_METRIC_FOOTNOTE_FORMULA_RE = re.compile(
    r"^(?P<body>.+?)\^\{?(?P<label>\d{1,3})\}?$"
)
_STANDALONE_FOOTNOTE_FORMULA_RE = re.compile(
    r"^(?:\{\})?\^\{?(?P<label>\d{1,3})\}?$"
)
_SIMPLE_NUMERIC_METRIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_CE_COMMAND_RE = re.compile(r"\\ce\{(?:[^{}]|\{[^{}]*\})*\}")
_CE_BLOCK_RE = re.compile(
    r"\$\$\s*(?P<command>\\ce\{(?:[^{}]|\{[^{}]*\})*\})\s*\$\$",
    re.DOTALL,
)
_CE_INLINE_RE = re.compile(
    r"(?<!\$)\$(?P<command>\\ce\{(?:[^{}]|\{[^{}]*\})*\})\$(?!\$)",
    re.DOTALL,
)
_TABLE_INLINE_MARKUP_RE = re.compile(
    r"(?P<citation>\$?\^\{(?P<citation_body>\[[^\]\r\n]+\])\}\$?)"
    r"|(?P<image>!\[(?P<image_alt>[^\]\r\n]*)\]\((?P<image_url>https?://[^)\s]+)\))"
    r"|(?P<bare_superscript>(?<![\\$])\^\{(?P<bare_superscript_body>[^{}\r\n]+)\})"
    r"|(?P<link>\[(?P<link_text>[^\]\r\n]+)\]\((?P<link_url>https?://[^)\s]+)\))"
    r"|(?P<code>`(?P<code_text>[^`\r\n]+)`)"
    r"|(?P<bold_ast>\*\*(?P<bold_ast_text>[^*\r\n]+)\*\*)"
    r"|(?P<bold_under>__(?P<bold_under_text>[^_\r\n]+)__)"
    r"|(?P<strike>~~(?P<strike_text>[^~\r\n]+)~~)"
    r"|(?P<italic>\*(?P<italic_text>[^*\r\n]+)\*)"
    r"|(?P<formula>(?<!\$)\$(?!\$)(?P<formula_body>[^$\r\n]+)\$(?!\$))"
)
_FENCE_LINE_RE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<body>[^\r\n]+)", re.MULTILINE)
_BARE_SUPERSCRIPT_RE = re.compile(r"(?<![\\$])\^\{(?P<body>[^{}\r\n]+)\}")
_PROTECTED_BARE_SUPERSCRIPT_RE = re.compile(
    r"<table\b[^>]*>.*?</table\s*>|"
    r"```.*?```|~~~.*?~~~|`[^`\r\n]*`|"
    r"\$\$.*?\$\$|(?<!\$)\$(?!\$)[^\r\n$]*\$(?!\$)",
    re.IGNORECASE | re.DOTALL,
)
@dataclass(frozen=True)
class DocumentPlan:
    """Local, side-effect-free-with-respect-to-DingTalk document plan."""

    title: str
    compatibility_markdown_path: str
    manifest_path: str
    summary_path: str
    create_arguments: tuple[str, ...]
    source_input_characters: int
    compatible_characters: int
    expected_write_chunks: int
    first_marker: str
    expected_counts: Mapping[str, Any]
    element_inventory: Mapping[str, int]
    element_audit: tuple[Mapping[str, Any], ...]
    table_rich_text_specs: tuple[Mapping[str, Any], ...]
    degradations: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]

    def to_safe_dict(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "title": self.title,
                "compatibility_markdown_path": self.compatibility_markdown_path,
                "manifest_path": self.manifest_path,
                "summary_path": self.summary_path,
                "create_arguments": list(self.create_arguments),
                "source_input_characters": self.source_input_characters,
                "compatible_characters": self.compatible_characters,
                "expected_write_chunks": self.expected_write_chunks,
                "first_marker": self.first_marker,
                "expected_counts": dict(self.expected_counts),
                "element_inventory": dict(self.element_inventory),
                "element_audit": [dict(item) for item in self.element_audit],
                "table_rich_text_table_count": len(self.table_rich_text_specs),
                "degradations": [dict(item) for item in self.degradations],
                "warnings": list(self.warnings),
            }
        )


def _degradation(category: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return redact_sensitive(
        {"category": category, "code": code, "message": message, **details}
    )


def _degradation_strings(items: Iterable[Mapping[str, Any]]) -> list[str]:
    return [f"{item.get('category')}: {item.get('message')}" for item in items]


def _write_text_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def _write_json_atomic(path: Path, value: Any) -> Path:
    safe = redact_sensitive(value)
    return _write_text_atomic(
        path,
        json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _validate_target(target: RouteTarget) -> Path:
    if target.route is not RouteName.DOCUMENT:
        raise ValueError("target.route must be RouteName.DOCUMENT")
    if not target.create_only:
        raise ValueError("the document route is create-only")
    evidence_dir = Path(target.evidence_dir).expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir


def _validate_source_hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("source.source_hash must be a SHA-256 hex digest")


def _read_text(path: str | None, label: str) -> str | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} does not exist or is not a file: {resolved}")
    return resolved.read_text(encoding="utf-8-sig")


def _unwrap_somark_json(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("pages"), list):
        return value
    if not isinstance(value, dict):
        return value
    task_data = value.get("data") if isinstance(value.get("data"), dict) else value
    result = task_data.get("result") if isinstance(task_data, dict) else None
    outputs = result.get("outputs") if isinstance(result, dict) else None
    payload = outputs.get("json") if isinstance(outputs, dict) else None
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload if isinstance(payload, (dict, list)) else value


def _read_json(path: str | None) -> Any | None:
    text = _read_text(path, "SoMark JSON artifact")
    if text is None:
        return None
    return _unwrap_somark_json(json.loads(text))


def _iter_blocks(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("type"), str):
            yield value
        for child in value.values():
            yield from _iter_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_blocks(child)


def _ordered_pages(value: Any) -> list[list[dict[str, Any]]]:
    pages = value.get("pages") if isinstance(value, dict) else None
    if not isinstance(pages, list):
        blocks = list(_iter_blocks(value))
        return [blocks] if blocks else []
    result: list[list[dict[str, Any]]] = []
    for page in pages:
        raw_blocks = page.get("blocks") if isinstance(page, dict) else None
        result.append([block for block in (raw_blocks or []) if isinstance(block, dict)])
    return result


def _content(block: Mapping[str, Any]) -> str:
    value = block.get("content")
    return value.strip() if isinstance(value, str) else ""


def _image_url(block: Mapping[str, Any]) -> str:
    value = block.get("img_url")
    return value.strip() if isinstance(value, str) else ""


def _single_line(value: str, limit: int = 500) -> str:
    clean = re.sub(r"\s+", " ", unescape(value)).strip()
    return clean[:limit]


def _page_number_like(value: str) -> bool:
    return bool(re.fullmatch(r"[\s\-\u2013\u2014]*(?:\d+|\d+\s*/\s*\d+)[\s\-\u2013\u2014]*", value))


def _fence_segments(markdown: str) -> list[tuple[bool, str]]:
    lines = markdown.splitlines(keepends=True)
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    in_fence = False
    marker: str | None = None

    def flush() -> None:
        if buffer:
            segments.append((in_fence, "".join(buffer)))
            buffer.clear()

    for line in lines:
        match = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
        candidate = match.group("marker") if match else None
        if not in_fence and candidate:
            flush()
            in_fence = True
            marker = candidate
            buffer.append(line)
        elif in_fence:
            buffer.append(line)
            if candidate and marker and candidate[0] == marker[0] and len(candidate) >= len(marker):
                flush()
                in_fence = False
                marker = None
        else:
            buffer.append(line)
    flush()
    return segments


def _replace_once_outside_fences(markdown: str, source: str, replacement: str) -> tuple[str, bool]:
    if not source:
        return markdown, False
    output: list[str] = []
    replaced = False
    for fenced, segment in _fence_segments(markdown):
        if not fenced and not replaced and source in segment:
            segment = segment.replace(source, replacement, 1)
            replaced = True
        output.append(segment)
    return "".join(output), replaced


def _remove_exact_block_outside_fences(markdown: str, source: str) -> tuple[str, bool]:
    """Remove one standalone block without touching the same text inline."""

    expected = [line.strip() for line in source.strip().splitlines()]
    if not expected:
        return markdown, False
    output: list[str] = []
    removed = False
    for fenced, segment in _fence_segments(markdown):
        if fenced or removed:
            output.append(segment)
            continue
        lines = segment.splitlines(keepends=True)
        width = len(expected)
        for start in range(0, len(lines) - width + 1):
            actual = [line.strip() for line in lines[start : start + width]]
            if actual == expected:
                del lines[start : start + width]
                removed = True
                break
        output.append("".join(lines))
    return "".join(output), removed


def _safe_code_language(value: str) -> str:
    value = value.strip().split()[0] if value.strip() else ""
    return value if re.fullmatch(r"[A-Za-z0-9_+.#-]{1,32}", value) else ""


def _is_fenced(markdown: str, content: str) -> bool:
    return any(fenced and content.strip() in segment for fenced, segment in _fence_segments(markdown))


def _transform_code(
    markdown: str,
    blocks: Sequence[Mapping[str, Any]],
    degradations: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[str, int, int]:
    recovered = 0
    isolated_images = 0
    for block_index, block in enumerate(blocks):
        if block.get("type") != "code":
            continue
        raw = _content(block)
        if not raw:
            degradations.append(
                _degradation("somark_parse", "empty_code_block", "SoMark code block had no content", block_index=block_index)
            )
            continue
        body = raw
        trailing_image = ""
        mixed = re.search(r"\s+(!\[[^\]]*\]\(https?://[^)\s]+\))\s*$", raw, re.DOTALL)
        if mixed:
            body = raw[: mixed.start()].rstrip()
            trailing_image = mixed.group(1)
            isolated_images += 1
            degradations.append(
                _degradation(
                    "somark_parse",
                    "qrcode_link_mixed_into_code",
                    "An image/QR reference mixed into SoMark code content was moved outside the code fence",
                    block_index=block_index,
                )
            )
        language_value = block.get("code_language")
        language = _safe_code_language(language_value if isinstance(language_value, str) else "")
        if not language:
            degradations.append(
                _degradation(
                    "adapter",
                    "empty_code_language",
                    "Code language was empty or unsafe; the fenced block uses no guessed language",
                    block_index=block_index,
                )
            )
        if _is_fenced(markdown, body):
            continue
        replacement = f"```{language}\n{body}\n```"
        if trailing_image:
            replacement += f"\n\n{trailing_image}\n\n> [二维码/图片说明] 该图片引用已从代码正文中隔离。"
        markdown, found = _replace_once_outside_fences(markdown, raw, replacement)
        if not found and body != raw:
            markdown, found = _replace_once_outside_fences(markdown, body, replacement)
        if found:
            recovered += 1
        else:
            warnings.append(f"Could not locate exact JSON code content in Markdown at block {block_index}")
            degradations.append(
                _degradation(
                    "adapter",
                    "code_text_not_located",
                    "Structured code content could not be fenced because its exact text was absent from Markdown",
                    block_index=block_index,
                )
            )
    return markdown, recovered, isolated_images


def _find_image_match(markdown: str, url: str) -> re.Match[str] | None:
    normalized = url.replace("\\", "/").split("?", 1)[0].lstrip("./")
    normalized_parts = tuple(part for part in normalized.split("/") if part)
    asset_key = normalized_parts[-2:] if len(normalized_parts) >= 2 else normalized_parts
    for match in _IMAGE_RE.finditer(markdown):
        candidate = match.group("url")
        candidate_normalized = candidate.replace("\\", "/").split("?", 1)[0].lstrip("./")
        candidate_parts = tuple(part for part in candidate_normalized.split("/") if part)
        candidate_key = candidate_parts[-2:] if len(candidate_parts) >= 2 else candidate_parts
        if candidate == url or (asset_key and candidate_key == asset_key):
            return match
    return None


def _count_table_citation_superscripts(markdown: str) -> int:
    """Count citation superscripts in HTML table cells without rewriting Markdown."""

    return sum(
        len(_TABLE_CITATION_SUPERSCRIPT_RE.findall(match.group("body")))
        for match in _TABLE_CELL_RE.finditer(markdown)
    )


def _append_text_token(
    tokens: list[dict[str, Any]],
    text: str,
    marks: Mapping[str, Any] | None = None,
    *,
    source_kind: str | None = None,
) -> None:
    if not text:
        return
    token = {"kind": "text", "text": text, "marks": dict(marks or {})}
    if source_kind:
        token["source_kind"] = source_kind
    if (
        tokens
        and tokens[-1].get("kind") == "text"
        and tokens[-1].get("marks") == token["marks"]
        and tokens[-1].get("source_kind") == token.get("source_kind")
    ):
        tokens[-1]["text"] = str(tokens[-1].get("text") or "") + text
        return
    tokens.append(token)


def _balanced_braces(value: str) -> bool:
    depth = 0
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _split_metric_formula_footnotes(
    tokens: Sequence[Mapping[str, Any]],
    footnote_labels: set[str],
) -> list[dict[str, Any]]:
    r"""Separate numeric metric formulas from their table-note superscripts.

    A source fragment such as ``$0.88\sim0.92^2$`` commonly means the numeric
    range ``0.88\sim0.92`` followed by table note 2, not ``0.92`` squared.  We
    only split a trailing numeric superscript when that label is declared by
    the note immediately following the table and the formula starts with a
    numeric metric.  General formulas such as ``$E=mc^2$`` remain untouched.
    """

    output: list[dict[str, Any]] = []
    for token in tokens:
        copied = dict(token)
        if copied.get("kind") != "formula" or not footnote_labels:
            output.append(copied)
            continue
        formula = str(copied.get("formula") or "").strip()
        standalone = _STANDALONE_FOOTNOTE_FORMULA_RE.fullmatch(formula)
        if standalone is not None and standalone.group("label") in footnote_labels:
            output.append(
                {
                    "kind": "text",
                    "text": standalone.group("label"),
                    "marks": {"vertAlign": "superscript"},
                    "source_kind": "metric_footnote",
                }
            )
            continue
        match = _METRIC_FOOTNOTE_FORMULA_RE.fullmatch(formula)
        if match is None:
            output.append(copied)
            continue
        body = match.group("body").strip()
        label = match.group("label")
        if (
            label not in footnote_labels
            or re.match(r"^[+-]?(?:\d|\.\d)", body) is None
            or "=" in body
            or not _balanced_braces(body)
        ):
            output.append(copied)
            continue
        if _SIMPLE_NUMERIC_METRIC_RE.fullmatch(body):
            output.append(
                {
                    "kind": "text",
                    "text": body,
                    "marks": dict(copied.get("marks") or {}),
                    "source_kind": "metric_value",
                }
            )
        else:
            output.append({**copied, "formula": body})
        output.append(
            {
                "kind": "text",
                "text": label,
                "marks": {"vertAlign": "superscript"},
                "source_kind": "metric_footnote",
            }
        )
    return output


def _bbox(value: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    raw = value.get("bbox")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 4:
        return None
    try:
        left, top, right, bottom = (float(raw[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _inside_region(candidate: Mapping[str, Any], region: Mapping[str, Any]) -> bool:
    candidate_box = _bbox(candidate)
    region_box = _bbox(region)
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
    page: Sequence[Mapping[str, Any]],
    marker_index: int,
) -> list[tuple[int, str]]:
    marker = page[marker_index]
    collected: list[tuple[int, str]] = []
    marker_box = _bbox(marker)
    for index in range(marker_index + 1, len(page)):
        candidate = page[index]
        if str(candidate.get("type") or "") != "text" or not _content(candidate):
            break
        if marker_box is not None and not _inside_region(candidate, marker):
            break
        collected.append((index, _content(candidate)))
        # Without geometry, retain the old conservative one-block fallback.
        if marker_box is None:
            break
    return collected


def _extract_ce_content(command: str) -> str:
    start = command.find("{")
    if start < 0:
        return command
    depth = 0
    for index in range(start, len(command)):
        character = command[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return command[start + 1 : index]
    return command[start + 1 :]


def _chemistry_plain_text(command: str) -> str:
    content = _extract_ce_content(command)

    def replace_arrow(match: re.Match[str]) -> str:
        labels = [item.strip() for item in re.findall(r"\[([^\]]*)\]", match.group("conditions")) if item.strip()]
        return f" →（{'；'.join(labels)}） " if labels else " → "

    content = re.sub(r"->(?P<conditions>(?:\[[^\]]*\])*)", replace_arrow, content)
    content = content.replace("<=>", " ⇌ ").replace("<->", " ⇌ ").replace("<-", " ← ")
    content = content.replace(r"\Delta", "Δ").replace(r"\uparrow", "↑").replace(r"\downarrow", "↓")
    return re.sub(r"\s+", " ", content).strip()


def _format_chemistry_condition(condition: str) -> str:
    condition = re.sub(r"\s+", " ", condition.strip())
    condition = condition.replace(r"\Delta", "Δ").replace(r"\uparrow", "↑").replace(r"\downarrow", "↓")
    condition = condition.replace("{", r"\{").replace("}", r"\}")
    return rf"\text{{{condition}}}"


def _format_polymer_repeat(side: str) -> str | None:
    match = re.fullmatch(r"\[(?P<body>[^\[\]]+)\](?P<count>[A-Za-z0-9]+)", side.strip())
    if match is None:
        return None
    body = match.group("body").strip()
    count = match.group("count").strip()
    if not body or not re.fullmatch(r"[A-Za-z0-9(){}\-=\u00b7.]+", body):
        return None
    if re.fullmatch(r"C(?:H\d*)?(?:C(?:H\d*)?)+", body) and "-" not in body and "=" not in body:
        body = re.sub(r"(?<=\d)(?=C)", "-", body)
    formatted = _format_chemistry_side(f"-{body}-")
    return rf"\left[{formatted}\right]_{{{count}}}" if formatted is not None else None


def _format_chemistry_side(side: str) -> str | None:
    side = re.sub(r"\s+", " ", side.strip())
    if not side:
        return None
    side = side.replace(r"\Delta", "Δ").replace(r"\uparrow", "↑").replace(r"\downarrow", "↓")
    if "[" in side or "]" in side:
        return _format_polymer_repeat(side)
    if "\\" in side or "^" in side or "_" in side:
        return None
    if not re.fullmatch(r"[A-Za-z0-9\s+\-=(){}\u00b7.↑↓°%Δ]+", side):
        return None
    side = side.replace("{", "(").replace("}", ")")
    side = re.sub(r"(?<=[A-Za-z)])(\d+)", r"_{\1}", side)
    side = re.sub(r"(?<=\))n\b", r"_n", side)
    side = side.replace("·", r"\cdot ").replace("↑", r"\uparrow ").replace("↓", r"\downarrow ").replace("°", r"^\circ ")
    return rf"\mathrm{{{side.strip()}}}"


def _chemistry_latex(command: str) -> str | None:
    content = _extract_ce_content(command).strip()
    arrows = list(re.finditer(r"(?P<arrow><=>|<->|<-|->)(?P<conditions>(?:\[[^\]]*\])*)", content))
    if len(arrows) > 1:
        return None
    if not arrows:
        return _format_chemistry_side(content)
    match = arrows[0]
    left = _format_chemistry_side(content[: match.start()])
    right = _format_chemistry_side(content[match.end() :])
    if left is None or right is None:
        return None
    conditions = [_format_chemistry_condition(item) for item in re.findall(r"\[([^\]]*)\]", match.group("conditions")) if item.strip()]
    arrow = match.group("arrow")
    if arrow == "->":
        if len(conditions) >= 2:
            arrow_latex = rf"\xrightarrow[{'; '.join(conditions[1:])}]{{{conditions[0]}}}"
        elif conditions:
            arrow_latex = rf"\xrightarrow{{{conditions[0]}}}"
        else:
            arrow_latex = r"\rightarrow"
    elif arrow == "<-":
        arrow_latex = rf"\xleftarrow{{{conditions[0]}}}" if conditions else r"\leftarrow"
    elif conditions:
        return None
    else:
        arrow_latex = r"\rightleftharpoons"
    return f"{left} {arrow_latex} {right}"


def _transform_chemistry(markdown: str) -> tuple[str, dict[str, int]]:
    stats = Counter()

    def replacement(command: str, *, block: bool) -> str:
        converted = _chemistry_latex(command)
        context = "block" if block else "inline"
        if converted is None:
            stats["chemistry_formulas_degraded"] += 1
            stats[f"chemistry_{context}_formulas_degraded"] += 1
            return _chemistry_plain_text(command)
        stats["chemistry_formulas_converted"] += 1
        stats[f"chemistry_{context}_formulas_converted"] += 1
        delimiter = "$$" if block else "$"
        return f"{delimiter}\n{converted}\n{delimiter}" if block else f"${converted}$"

    markdown = _CE_BLOCK_RE.sub(lambda match: replacement(match.group("command"), block=True), markdown)
    markdown = _CE_INLINE_RE.sub(lambda match: replacement(match.group("command"), block=False), markdown)
    markdown = _CE_COMMAND_RE.sub(lambda match: replacement(match.group(0), block=False), markdown)
    return markdown, dict(stats)


def _transform_bare_superscripts(markdown: str) -> tuple[str, int]:
    """Render safe ordinary ``^{...}`` fragments as DingTalk inline formulas."""
    repaired = 0

    def repair_plain(fragment: str) -> str:
        nonlocal repaired

        def replace(match: re.Match[str]) -> str:
            nonlocal repaired
            repaired += 1
            return f"${match.group(0)}$"

        return _BARE_SUPERSCRIPT_RE.sub(replace, fragment)

    output: list[str] = []
    position = 0
    for protected in _PROTECTED_BARE_SUPERSCRIPT_RE.finditer(markdown):
        output.append(repair_plain(markdown[position : protected.start()]))
        output.append(protected.group(0))
        position = protected.end()
    output.append(repair_plain(markdown[position:]))
    return "".join(output), repaired


def _parse_inline_tokens(
    value: str,
    inherited_marks: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compile simple inline markup into a source-neutral rich-text token stream."""

    marks = dict(inherited_marks or {})
    tokens: list[dict[str, Any]] = []
    position = 0
    while position < len(value):
        match = _TABLE_INLINE_MARKUP_RE.search(value, position)
        if match is None:
            _append_text_token(tokens, value[position:], marks)
            break
        if match.start() > position:
            _append_text_token(tokens, value[position : match.start()], marks)

        group = next(
            name
            for name in (
                "citation",
                "image",
                "bare_superscript",
                "link",
                "code",
                "bold_ast",
                "bold_under",
                "strike",
                "italic",
                "formula",
            )
            if match.group(name) is not None
        )
        if group == "citation":
            citation_marks = dict(marks)
            citation_marks["vertAlign"] = "superscript"
            _append_text_token(
                tokens,
                match.group("citation_body"),
                citation_marks,
                source_kind="citation",
            )
        elif group == "image":
            _append_table_image_tokens(
                tokens,
                src=match.group("image_url"),
                alt=match.group("image_alt"),
                marks=marks,
            )
        elif group == "bare_superscript":
            superscript_marks = dict(marks)
            superscript_marks["vertAlign"] = "superscript"
            _append_text_token(
                tokens,
                match.group("bare_superscript_body"),
                superscript_marks,
                source_kind="bare_superscript",
            )
        elif group == "link":
            tokens.append(
                {
                    "kind": "link",
                    "text": match.group("link_text"),
                    "href": match.group("link_url"),
                }
            )
        elif group == "code":
            tokens.append({"kind": "inline_code", "text": match.group("code_text")})
        elif group in {"bold_ast", "bold_under", "strike", "italic"}:
            inner_group = {
                "bold_ast": "bold_ast_text",
                "bold_under": "bold_under_text",
                "strike": "strike_text",
                "italic": "italic_text",
            }[group]
            nested_marks = dict(marks)
            nested_marks[{"strike": "strike", "italic": "italic"}.get(group, "bold")] = True
            tokens.extend(_parse_inline_tokens(match.group(inner_group), nested_marks))
        elif group == "formula":
            formula = match.group("formula_body").strip()
            if formula and _balanced_braces(formula):
                tokens.append({"kind": "formula", "formula": formula})
            else:
                _append_text_token(tokens, match.group(0), marks)
        else:
            _append_text_token(tokens, match.group(0), marks)
        position = match.end()
    return tokens


def _append_table_image_tokens(
    tokens: list[dict[str, Any]],
    *,
    src: str,
    alt: str,
    marks: Mapping[str, Any] | None = None,
) -> None:
    """Keep a source table-image description visibly below its image."""

    description = re.sub(r"\s+", " ", alt).strip()
    tokens.append({"kind": "image", "src": src, "alt": description})
    if not description:
        return
    tokens.append({"kind": "line_break"})
    _append_text_token(
        tokens,
        description,
        marks or {},
        source_kind="table_image_description",
    )
    # A following source fragment must not run into the description. The
    # cell finalizer removes this break when the image ends the cell.
    tokens.append({"kind": "line_break"})


def _token_feature_counts(tokens: Sequence[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for token in tokens:
        kind = str(token.get("kind") or "")
        if kind == "text":
            marks = token.get("marks") if isinstance(token.get("marks"), Mapping) else {}
            for key in (
                "bold",
                "italic",
                "strike",
                "dstrike",
                "underline",
                "color",
                "highlight",
                "shd",
                "sz",
                "fonts",
                "spacing",
            ):
                if key in marks and marks.get(key) not in (None, False, ""):
                    counts[{"sz": "font_size", "fonts": "font"}.get(key, key)] += 1
            vert_align = marks.get("vertAlign")
            if vert_align in {"superscript", "subscript"}:
                counts[str(vert_align)] += 1
            if token.get("source_kind") == "citation":
                counts["citation_superscript"] += 1
        elif kind:
            counts[kind] += 1
    return counts


def _tokens_visible_text(tokens: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for token in tokens:
        kind = str(token.get("kind") or "")
        if kind in {"text", "link", "inline_code"}:
            parts.append(str(token.get("text") or ""))
        elif kind == "formula":
            parts.append(f"${token.get('formula') or ''}$")
        elif kind == "line_break":
            parts.append("\n")
        elif kind == "image":
            parts.append(str(token.get("alt") or ""))
    return "".join(parts)


def _hex_color(value: str) -> str | None:
    candidate = value.strip().casefold()
    if re.fullmatch(r"#[0-9a-f]{6}", candidate):
        return candidate
    if re.fullmatch(r"#[0-9a-f]{3}", candidate):
        return "#" + "".join(character * 2 for character in candidate[1:])
    return None


def _css_declarations(value: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for item in value.split(";"):
        if ":" not in item:
            continue
        key, raw = item.split(":", 1)
        if key.strip() and raw.strip():
            declarations[key.strip().casefold()] = raw.strip()
    return declarations


def _marks_from_html(tag: str, attrs: Mapping[str, str], base: Mapping[str, Any]) -> dict[str, Any]:
    marks = dict(base)
    if tag in {"b", "strong"}:
        marks["bold"] = True
    elif tag in {"i", "em"}:
        marks["italic"] = True
    elif tag == "u":
        marks["underline"] = {"value": "single"}
    elif tag in {"s", "strike", "del"}:
        marks["strike"] = True
    elif tag == "sup":
        marks["vertAlign"] = "superscript"
    elif tag == "sub":
        marks["vertAlign"] = "subscript"

    declarations = _css_declarations(attrs.get("style", ""))
    weight = declarations.get("font-weight", "").casefold()
    if weight in {"bold", "bolder"} or (weight.isdigit() and int(weight) >= 600):
        marks["bold"] = True
    if declarations.get("font-style", "").casefold() in {"italic", "oblique"}:
        marks["italic"] = True
    decoration = declarations.get("text-decoration", "").casefold()
    if "underline" in decoration:
        marks["underline"] = {"value": "single"}
    if "line-through" in decoration:
        marks["strike"] = True
    color = _hex_color(declarations.get("color", "") or attrs.get("color", ""))
    if color:
        marks["color"] = color
    highlight = declarations.get("background-color", "")
    if highlight:
        marks["highlight"] = highlight
    font_family = declarations.get("font-family", "") or attrs.get("face", "")
    if font_family:
        family = font_family.split(",", 1)[0].strip().strip("\"'")
        if family:
            marks["fonts"] = {
                "ascii": family,
                "hAnsi": family,
                "cs": family,
                "eastAsia": family,
            }
    size = declarations.get("font-size", "")
    size_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(px|pt)", size.casefold())
    if size_match:
        marks["sz"] = float(size_match.group(1))
        marks["szUnit"] = size_match.group(2)
    vertical = declarations.get("vertical-align", "").casefold()
    if vertical in {"super", "superscript"}:
        marks["vertAlign"] = "superscript"
    elif vertical in {"sub", "subscript"}:
        marks["vertAlign"] = "subscript"
    spacing = declarations.get("letter-spacing", "")
    spacing_match = re.fullmatch(r"(-?[0-9]+(?:\.[0-9]+)?)(px|pt)", spacing.casefold())
    if spacing_match:
        numeric = float(spacing_match.group(1))
        marks["spacing"] = numeric * 0.75 if spacing_match.group(2) == "px" else numeric
    return marks


class _TableRichTextHTMLParser(HTMLParser):
    """Extract row-major table cells and their supported inline presentation."""

    def __init__(self, metric_footnote_labels: set[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[dict[str, Any]] = []
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._marks: dict[str, Any] = {}
        self._link: str | None = None
        self._inline_code = False
        self._contexts: list[tuple[str, dict[str, Any], str | None, bool]] = []
        self._metric_footnote_labels = set(metric_footnote_labels or set())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag == "tr":
            self._row = []
            self.rows.append(self._row)
            return
        if tag in {"td", "th"}:
            if self._row is None:
                self._row = []
                self.rows.append(self._row)
            declarations = _css_declarations(attributes.get("style", ""))
            cell_attrs: dict[str, Any] = {}
            paragraph_attrs: dict[str, Any] = {}
            fill = declarations.get("background-color", "")
            if fill:
                cell_attrs["fill"] = fill
            vertical = declarations.get("vertical-align", "").casefold()
            if vertical in {"top", "middle", "bottom"}:
                cell_attrs["vAlign"] = vertical
            elif vertical == "center":
                cell_attrs["vAlign"] = "middle"
            alignment = declarations.get("text-align", "").casefold()
            if alignment in {"left", "center", "right", "justify"}:
                paragraph_attrs["jc"] = alignment
            self._cell = {
                "tokens": [],
                "raw_parts": [],
                "cell_attrs": cell_attrs,
                "paragraph_attrs": paragraph_attrs,
                "rowspan": _positive_span(attributes.get("rowspan")),
                "colspan": _positive_span(attributes.get("colspan")),
            }
            self._marks = {"bold": True} if tag == "th" else {}
            self._link = None
            self._inline_code = False
            self._contexts = []
            return
        if self._cell is None:
            return
        if tag == "br":
            self._cell["tokens"].append({"kind": "line_break"})
            self._cell["raw_parts"].append("\n")
            return
        if tag == "img":
            source = attributes.get("src", "").strip()
            if source:
                _append_table_image_tokens(
                    self._cell["tokens"],
                    src=source,
                    alt=attributes.get("alt", ""),
                    marks=self._marks,
                )
            return
        if tag in {"p", "div"}:
            tokens = self._cell["tokens"]
            if tokens and tokens[-1].get("kind") != "line_break":
                tokens.append({"kind": "line_break"})
                self._cell["raw_parts"].append("\n")
            return
        self._contexts.append((tag, dict(self._marks), self._link, self._inline_code))
        self._marks = _marks_from_html(tag, attributes, self._marks)
        if tag == "a" and attributes.get("href", "").strip():
            self._link = attributes["href"].strip()
        if tag == "code":
            self._inline_code = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"}:
            if self._cell is not None:
                tokens = self._cell["tokens"]
                while tokens and tokens[-1].get("kind") == "line_break":
                    tokens.pop()
                self._cell["raw_text"] = "".join(self._cell.pop("raw_parts"))
                self._cell["visible_text"] = _tokens_visible_text(tokens)
                features = _token_feature_counts(tokens)
                features.update(
                    {
                        "cell_fill": int("fill" in self._cell["cell_attrs"]),
                        "cell_vertical_alignment": int("vAlign" in self._cell["cell_attrs"]),
                        "paragraph_alignment": int("jc" in self._cell["paragraph_attrs"]),
                    }
                )
                self._cell["feature_counts"] = dict(+features)
                self.cells.append(self._cell)
                if self._row is not None:
                    self._row.append(self._cell)
            self._cell = None
            self._marks = {}
            self._link = None
            self._inline_code = False
            self._contexts = []
            return
        if tag == "tr":
            self._row = None
            return
        if tag in {"br", "img", "p", "div"}:
            return
        if self._cell is None or not self._contexts or self._contexts[-1][0] != tag:
            return
        context_tag, marks, link, inline_code = self._contexts.pop()
        self._marks = marks
        self._link = link
        self._inline_code = inline_code

    def handle_data(self, data: str) -> None:
        if self._cell is None or not data:
            return
        self._cell["raw_parts"].append(data)
        tokens = self._cell["tokens"]
        if self._link:
            if tokens and tokens[-1].get("kind") == "link" and tokens[-1].get("href") == self._link:
                tokens[-1]["text"] = str(tokens[-1].get("text") or "") + data
            else:
                tokens.append({"kind": "link", "text": data, "href": self._link})
        elif self._inline_code:
            if tokens and tokens[-1].get("kind") == "inline_code":
                tokens[-1]["text"] = str(tokens[-1].get("text") or "") + data
            else:
                tokens.append({"kind": "inline_code", "text": data})
        else:
            parsed_tokens = _split_metric_formula_footnotes(
                _parse_inline_tokens(data, self._marks),
                self._metric_footnote_labels,
            )
            for token in parsed_tokens:
                if token.get("kind") == "text":
                    _append_text_token(
                        tokens,
                        str(token.get("text") or ""),
                        token.get("marks") if isinstance(token.get("marks"), Mapping) else {},
                        source_kind=str(token.get("source_kind")) if token.get("source_kind") else None,
                    )
                else:
                    tokens.append(token)


def _positive_span(value: Any) -> int:
    try:
        return max(1, int(str(value or "1")))
    except (TypeError, ValueError):
        return 1


def _assign_source_logical_positions(rows: Sequence[Sequence[dict[str, Any]]]) -> None:
    """Attach logical row/column origins while respecting rowspan/colspan."""

    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        column_index = 0
        for cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            cell["row_index"] = row_index
            cell["column_index"] = column_index
            rowspan = _positive_span(cell.get("rowspan"))
            colspan = _positive_span(cell.get("colspan"))
            for target_row in range(row_index, row_index + rowspan):
                for target_column in range(column_index, column_index + colspan):
                    occupied.add((target_row, target_column))
            column_index += colspan


def _table_note_labels(markdown: str, table_end: int, next_boundary: int) -> set[str]:
    labels: set[str] = set()
    tail = markdown[table_end:next_boundary]
    heading = _HEADING_RE.search(tail)
    if heading is not None:
        tail = tail[: heading.start()]
    for line in tail.splitlines():
        if re.match(r"^\s*(?:注|Note)\s*[:：]", line, re.IGNORECASE) is None:
            continue
        labels.update(match.group("label") for match in _TABLE_NOTE_SUPERSCRIPT_RE.finditer(line))
    return labels


def _sanitize_conflicting_header_rowspan(source: str) -> tuple[str, int]:
    """Remove only a header rowspan that conflicts with a complete next row.

    This mirrors the narrowly scoped Feishu demo repair.  It avoids an import
    expanding a malformed two-row header into extra physical columns while
    preserving ordinary merged cells everywhere else.
    """

    parser = _TableRichTextHTMLParser()
    parser.feed(source)
    parser.close()
    if len(parser.rows) < 2:
        return source, 0
    nominal_width = max(
        (sum(_positive_span(cell.get("colspan")) for cell in row) for row in parser.rows),
        default=0,
    )
    first_width = sum(_positive_span(cell.get("colspan")) for cell in parser.rows[0])
    second_width = sum(_positive_span(cell.get("colspan")) for cell in parser.rows[1])
    if (
        not nominal_width
        or first_width < nominal_width
        or second_width < nominal_width
        or not any(_positive_span(cell.get("rowspan")) > 1 for cell in parser.rows[0])
    ):
        return source, 0
    first_row = re.search(r"<tr\b[^>]*>.*?</tr\s*>", source, re.IGNORECASE | re.DOTALL)
    if first_row is None:
        return source, 0
    cleaned, count = re.subn(
        r"\s+rowspan\s*=\s*(?:(['\"])\d+\1|\d+)",
        "",
        first_row.group(0),
        flags=re.IGNORECASE,
    )
    if not count:
        return source, 0
    return source[: first_row.start()] + cleaned + source[first_row.end() :], count


def _sanitize_conflicting_table_headers(markdown: str) -> tuple[str, int]:
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        cleaned, count = _sanitize_conflicting_header_rowspan(match.group(0))
        removed += count
        return cleaned

    return _HTML_TABLE_RE.sub(replace, markdown), removed


def _isolate_table_image_paragraphs(markdown: str) -> tuple[str, int]:
    """Restore the table-cell image form verified by earlier live imports."""
    isolated = 0

    def rewrite_table(table_match: re.Match[str]) -> str:
        nonlocal isolated

        def rewrite_image(image_match: re.Match[str]) -> str:
            nonlocal isolated
            isolated += 1
            alt = image_match.group("alt")
            url = image_match.group("url")
            return f"\n\n![{alt}]({url})\n\n"

        return _IMAGE_RE.sub(rewrite_image, table_match.group(0))

    return _HTML_TABLE_RE.sub(rewrite_table, markdown), isolated


def _extract_table_rich_text_specs(
    markdown: str,
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, int]]:
    specs: list[Mapping[str, Any]] = []
    totals: Counter[str] = Counter()
    table_matches = list(_HTML_TABLE_RE.finditer(markdown))
    for table_index, table_match in enumerate(table_matches):
        next_boundary = (
            table_matches[table_index + 1].start()
            if table_index + 1 < len(table_matches)
            else len(markdown)
        )
        parser = _TableRichTextHTMLParser(
            _table_note_labels(markdown, table_match.end(), next_boundary)
        )
        parser.feed(table_match.group(0))
        parser.close()
        _assign_source_logical_positions(parser.rows)
        rich_cells: list[dict[str, Any]] = []
        for cell_index, cell in enumerate(parser.cells):
            features = Counter(cell.get("feature_counts") or {})
            if not sum(features.values()):
                continue
            rich_cell = {**cell, "cell_index": cell_index}
            rich_cells.append(rich_cell)
            totals.update(features)
        if rich_cells:
            specs.append(
                {
                    "table_index": table_index,
                    "cell_count": len(parser.cells),
                    "cells": rich_cells,
                }
            )
    return tuple(specs), dict(sorted(totals.items()))


def _validate_document_image_urls(markdown: str) -> None:
    local_urls = [
        match.group("url")
        for match in _IMAGE_RE.finditer(markdown)
        if urlsplit(match.group("url")).scheme.casefold() not in {"http", "https"}
    ]
    if not local_urls:
        return
    preview = ", ".join(dict.fromkeys(local_urls[:3]))
    raise ValueError(
        "the DingTalk document route requires HTTP(S) image URLs; "
        f"found local/relative image references: {preview}. "
        "Parse once with Markdown + JSON and element_formats.image=url; do not request ZIP."
    )


def _augment_images(
    markdown: str,
    blocks: Sequence[Mapping[str, Any]],
    degradations: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[str, list[dict[str, Any]], int]:
    plans: list[dict[str, Any]] = []
    visible_descriptions = 0
    for block_index, block in enumerate(blocks):
        block_type = str(block.get("type") or "")
        if block_type not in {"figure", "stamp", "qrcode", *_CHEMICAL_ELEMENT_TYPES}:
            continue
        url = _image_url(block)
        description = _single_line(_content(block))
        if block_type in _CHEMICAL_ELEMENT_TYPES and description and not url:
            continue
        label = {
            "figure": "图片说明",
            "stamp": "印章说明",
            "qrcode": "二维码说明",
            "cs": "化学式说明",
            "cs_equation": "化学式说明",
            "chemical_formula": "化学式说明",
            "chemical_structure": "化学式说明",
            "chemical_equation": "化学式说明",
        }[block_type]
        plan = {
            "source_block_index": block_index,
            "source_type": block_type,
            "source_url": url,
            "description": description,
            "representation": (
                "image_plus_adjacent_editable_paragraph"
                if description
                else "image_only"
            ),
        }
        plans.append(plan)
        if not url:
            if not description:
                degradations.append(
                    _degradation(
                        "somark_parse",
                        f"empty_{block_type}",
                        f"SoMark {block_type} evidence had neither content nor an image URL",
                        block_index=block_index,
                    )
                )
            continue
        match = _find_image_match(markdown, url)
        if match is None:
            image_markdown = f"![{description}]({url})"
            markdown = markdown.rstrip() + f"\n\n{image_markdown}\n"
            match = _find_image_match(markdown, url)
            warnings.append(f"Image URL for {block_type} block {block_index} was appended because it was absent from Markdown")
            degradations.append(
                _degradation(
                    "adapter",
                    "image_reference_recovered",
                    "A structured image reference absent from Markdown was appended to preserve content",
                    block_index=block_index,
                )
            )
        if description:
            marker = f"> [{label}] {description}"
            nearby = markdown[match.end() : match.end() + len(marker) + 32] if match else ""
            if marker not in nearby:
                assert match is not None
                markdown = markdown[: match.end()] + f"\n\n{marker}" + markdown[match.end() :]
            visible_descriptions += 1
    return markdown, plans, visible_descriptions


def _associate_captions(
    markdown: str,
    blocks: Sequence[Mapping[str, Any]],
    degradations: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    relations: list[dict[str, Any]] = []
    owner_indexes = {
        owner_type: [
            index
            for index, candidate in enumerate(blocks)
            if candidate.get("type") == owner_type
        ]
        for owner_type in ("figure", "table")
    }
    for block_index, block in enumerate(blocks):
        block_type = str(block.get("type") or "")
        if block_type in {"figure_caption", "table_caption"}:
            owner_type = "figure" if block_type == "figure_caption" else "table"
            candidates = owner_indexes[owner_type]
            owner_index = (
                min(
                    candidates,
                    key=lambda index: (
                        abs(index - block_index),
                        0 if index < block_index else 1,
                    ),
                )
                if candidates
                else None
            )
            caption = _content(block)
            relation = {
                "caption_block_index": block_index,
                "caption_type": block_type,
                "owner_type": owner_type,
                "owner_block_index": owner_index,
                "caption": caption,
                "native_binding_claimed": False,
            }
            relations.append(relation)
            if caption and caption not in markdown:
                anchor_index = owner_index
                anchor = blocks[anchor_index] if anchor_index is not None else None
                inserted = False
                if owner_type == "figure" and anchor is not None:
                    match = _find_image_match(markdown, _image_url(anchor))
                    if match:
                        markdown = markdown[: match.end()] + f"\n\n{caption}" + markdown[match.end() :]
                        inserted = True
                elif owner_type == "table" and anchor is not None:
                    table_text = _content(anchor)
                    if table_text and table_text in markdown:
                        end = markdown.index(table_text) + len(table_text)
                        markdown = markdown[:end] + f"\n\n{caption}" + markdown[end:]
                        inserted = True
                if not inserted:
                    markdown = markdown.rstrip() + f"\n\n{caption}\n"
                    warnings.append(f"Caption block {block_index} was appended because its owner anchor was not unique")
            degradations.append(
                _degradation(
                    "dingtalk_model",
                    "caption_as_adjacent_paragraph",
                    f"{block_type} content is retained as an adjacent editable paragraph without claiming a native caption relationship",
                    block_index=block_index,
                    owner_block_index=owner_index,
                )
            )
    return markdown, relations


def _choice_list(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return "\n".join(line if re.match(r"^[-*+]\s+", line) else f"- {line}" for line in lines)


def _transform_semantic_text(
    markdown: str,
    pages: Sequence[Sequence[Mapping[str, Any]]],
    degradations: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    adjacency: list[dict[str, Any]] = []
    stats = Counter()
    note_number = 0
    for page_index, page in enumerate(pages):
        for block_index, block in enumerate(page):
            block_type = str(block.get("type") or "")
            content = _content(block)
            if block_type == "choice" and content:
                markdown, changed = _replace_once_outside_fences(markdown, content, _choice_list(content))
                stats["choices_listed"] += int(changed)
                degradations.append(
                    _degradation("dingtalk_model", "choice_as_list", "Choice content is retained as an editable list rather than a native choice control", page_index=page_index, block_index=block_index)
                )
            elif block_type == "reference":
                # SoMark uses empty reference blocks as page/column-level
                # bibliography regions.  The visible bibliography entries are
                # already ordinary Markdown text blocks.  Mirroring the Feishu
                # demo, retain the structural count in the element audit but
                # never turn the next bibliography entry into a blockquote.
                stats["reference_regions_ignored"] += 1
                if content and content not in markdown:
                    warnings.append(
                        f"Reference region content was absent from Markdown at page {page_index} block {block_index}"
                    )
            elif block_type == "footnote":
                visible = content
                source_indexes = [block_index]
                if not visible:
                    footnote_blocks = _footnote_text_blocks(page, block_index)
                    source_indexes = [index for index, _ in footnote_blocks]
                    visible = "\n\n".join(value for _, value in footnote_blocks)
                if not visible:
                    degradations.append(
                        _degradation("somark_parse", "empty_footnote_marker", "SoMark footnote marker had no content and no adjacent text evidence", page_index=page_index, block_index=block_index)
                    )
                    continue
                note_number += 1
                label = f"尾注 {note_number}"
                stats["footnotes_numbered"] += 1
                visible_lines = visible.splitlines()
                replacement_lines = [f"> [{label}] {visible_lines[0]}"]
                replacement_lines.extend(">" if not line else f"> {line}" for line in visible_lines[1:])
                replacement = "\n".join(replacement_lines)
                markdown, changed = _replace_once_outside_fences(markdown, visible, replacement)
                if not changed:
                    warnings.append(f"Could not locate adjacent footnote text in Markdown at page {page_index} blocks {source_indexes}")
                adjacency.append(
                    {
                        "marker_type": "footnote",
                        "page_index": page_index,
                        "marker_block_index": block_index,
                        "content_block_index": source_indexes[0] if len(source_indexes) == 1 else None,
                        "content_block_indexes": source_indexes,
                        "label": label,
                        "content": visible,
                        "native_object_claimed": False,
                    }
                )
                degradations.append(
                    _degradation("dingtalk_model", "footnote_as_editable_endnote", "footnote is retained as numbered editable text with adjacency recorded in the manifest", page_index=page_index, block_index=block_index)
                )
            elif block_type in {"header", "footer"} and content:
                markdown, changed = _remove_exact_block_outside_fences(markdown, content)
                stats["header_footer_filtered"] += int(changed)
            elif block_type == "blank":
                if content:
                    markdown, changed = _replace_once_outside_fences(markdown, content, f"[填空] {content}")
                    stats["blanks_labeled"] += int(changed)
                    degradations.append(
                        _degradation("dingtalk_model", "blank_as_placeholder", "Blank content is retained as editable placeholder text rather than an interactive blank", page_index=page_index, block_index=block_index)
                    )
                else:
                    degradations.append(
                        _degradation("somark_parse", "empty_blank", "SoMark blank block had no content; no DingTalk loss is claimed", page_index=page_index, block_index=block_index)
                    )
            elif block_type == "sider":
                if not content and not _image_url(block):
                    degradations.append(
                        _degradation("somark_parse", "empty_sider", "SoMark sider block had no content or image evidence; no DingTalk loss is claimed", page_index=page_index, block_index=block_index)
                    )
                elif content:
                    markdown, changed = _replace_once_outside_fences(markdown, content, f"> [侧边栏] {content}")
                    stats["siders_quoted"] += int(changed)
                    degradations.append(
                        _degradation("dingtalk_model", "sider_as_blockquote", "Sider content is retained as an editable blockquote rather than a native side layout", page_index=page_index, block_index=block_index)
                    )
    return markdown, adjacency, dict(stats)


def _markdown_counts(markdown: str) -> dict[str, Any]:
    heading_levels = Counter(len(match.group("marks")) for match in _HEADING_RE.finditer(markdown))
    html_tables = len(_HTML_TABLE_RE.findall(markdown))
    gfm_tables = len(
        re.findall(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$", markdown, re.MULTILINE)
    )
    fence_lines = sum(1 for line in markdown.splitlines() if _FENCE_LINE_RE.match(line))
    return {
        "characters": len(markdown),
        "headings": sum(heading_levels.values()),
        "heading_levels": {str(level): heading_levels.get(level, 0) for level in range(1, 7)},
        "images": len(_IMAGE_RE.findall(markdown)),
        "tables": html_tables + gfm_tables,
        "html_tables": html_tables,
        "gfm_tables": gfm_tables,
        "code_blocks": fence_lines // 2,
        "inline_formula_markers": len(re.findall(r"(?<!\$)\$(?!\$)[^\r\n$]+\$(?!\$)", markdown)),
        "block_formula_markers": len(re.findall(r"\$\$.*?\$\$", markdown, re.DOTALL)),
    }


def _plain_marker(value: str) -> str:
    value = _IMAGE_RE.sub(lambda match: match.group("alt"), value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"^[#>\-*+\d.)\s]+", "", value)
    value = re.sub(r"[*_`~\[\]]", "", value)
    return _single_line(value, 160)


def _first_marker(markdown: str) -> str:
    heading = _HEADING_RE.search(markdown)
    if heading:
        return _plain_marker(heading.group("body"))
    for line in markdown.splitlines():
        marker = _plain_marker(line)
        if marker:
            return marker
    return ""


def _element_audit(inventory: Mapping[str, int]) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    alias_counts = Counter(inventory)
    chemical_count = sum(alias_counts.get(key, 0) for key in _CHEMICAL_ELEMENT_TYPES)
    for legacy_type in LEGACY_ELEMENT_TYPES_21:
        if legacy_type == "cate_item":
            status = "retired_product_taxonomy"
            count = alias_counts.get("cate_item", 0)
        elif legacy_type in {"chemical_structure", "chemical_equation"}:
            status = "merged_into_current_chemical_formula"
            count = chemical_count
        elif legacy_type == "qrcode" and alias_counts.get("qrcode", 0) == 0:
            status = "sample_gap_not_dingtalk_loss"
            count = 0
        else:
            count = alias_counts.get(legacy_type, 0)
            status = "observed" if count else "not_observed"
        audit.append(
            {
                "legacy_type": legacy_type,
                "count": count,
                "status": status,
                "content_preservation": "tracked",
                "semantic_preservation": "native_or_degraded_per_manifest",
            }
        )
    return audit


def _convert(
    markdown: str | None,
    json_data: Any | None,
) -> tuple[str, dict[str, Any], tuple[Mapping[str, Any], ...]]:
    degradations: list[dict[str, Any]] = []
    warnings: list[str] = []
    if markdown is None or json_data is None:
        raise ValueError(
            "the document route requires both explicit SoMark Markdown and JSON artifacts"
        )
    pages = _ordered_pages(json_data)
    blocks = [block for page in pages for block in page]
    inventory = Counter(str(block.get("type") or "unknown") for block in blocks)

    original_characters = len(markdown)
    markdown, conflicting_header_rowspans_removed = _sanitize_conflicting_table_headers(markdown)
    markdown, chemistry_stats = _transform_chemistry(markdown)
    table_rich_text_specs, table_rich_text_features = _extract_table_rich_text_specs(markdown)
    detected_table_superscripts = int(table_rich_text_features.get("citation_superscript", 0))
    markdown, table_images_isolated = _isolate_table_image_paragraphs(markdown)
    markdown, bare_superscripts_wrapped = _transform_bare_superscripts(markdown)
    markdown, recovered_code, isolated_images = _transform_code(markdown, blocks, degradations, warnings)
    markdown, image_plans, visible_descriptions = _augment_images(markdown, blocks, degradations, warnings)
    markdown, caption_relations = _associate_captions(markdown, blocks, degradations, warnings)
    markdown, adjacency, semantic_stats = _transform_semantic_text(markdown, pages, degradations, warnings)

    if inventory.get("qrcode", 0) == 0:
        degradations.append(
            _degradation("somark_parse", "qrcode_sample_gap", "No independent qrcode block was present; this is a SoMark/sample evidence gap, not a DingTalk loss")
        )
    if inventory.get("cate_item", 0) == 0:
        degradations.append(
            _degradation("somark_parse", "cate_item_retired", "cate_item is retained only in the legacy 21-type audit and is not treated as a current missing DingTalk element")
        )
    if inventory.get("figure_caption", 0) or inventory.get("table_caption", 0):
        pass
    if inventory.get("table", 0):
        degradations.append(
            _degradation("markdown_jsonml_projection", "table_span_requires_jsonml", "Merged-cell semantics must be verified from JSONML; Markdown readback must not reconstruct rowSpan/colSpan")
        )
    if inventory.get("figure", 0):
        degradations.append(
            _degradation("markdown_jsonml_projection", "image_alt_not_markdown_fact", "Image descriptions are saved in the manifest and adjacent editable text because Markdown readback may drop alt text")
        )
    if inventory.get("equation", 0):
        degradations.append(
            _degradation("markdown_jsonml_projection", "block_formula_normalization", "Block formulas may normalize to inline-looking Markdown while preserving formula source in JSONML")
        )
    chemical_text_formula_count = sum(
        1
        for block in blocks
        if str(block.get("type") or "") in _CHEMICAL_ELEMENT_TYPES
        and bool(_content(block))
        and not bool(_image_url(block))
    )
    if chemistry_stats.get("chemistry_formulas_degraded", 0):
        warnings.append("Some mhchem formulas could not be represented as native formulas and were preserved as readable text")
        degradations.append(
            _degradation(
                "dingtalk_model",
                "chemistry_formula_fallback",
                "Unsupported mhchem syntax was preserved as readable text",
                count=int(chemistry_stats["chemistry_formulas_degraded"]),
            )
        )

    _validate_document_image_urls(markdown)
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown).strip()
    summary = {
        "source_input_characters": original_characters,
        "element_inventory": dict(sorted(inventory.items())),
        "legacy_element_types": list(LEGACY_ELEMENT_TYPES_21),
        "current_element_types": list(CURRENT_ELEMENT_TYPES_19),
        "element_audit": _element_audit(inventory),
        "image_plans": image_plans,
        "caption_relations": caption_relations,
        "note_reference_adjacency": adjacency,
        "transformations": {
            "code_blocks_recovered": recovered_code,
            "mixed_code_images_isolated": isolated_images,
            "visible_image_descriptions": visible_descriptions,
            "chemical_text_formulas": chemical_text_formula_count,
            "bare_superscripts_wrapped": bare_superscripts_wrapped,
            **chemistry_stats,
            "table_superscripts_detected": detected_table_superscripts,
            "table_images_isolated": table_images_isolated,
            "table_rich_text_features": table_rich_text_features,
            "table_rich_text_candidates": sum(
                count
                for feature, count in table_rich_text_features.items()
                if feature != "citation_superscript"
            ),
            "conflicting_header_rowspans_removed": conflicting_header_rowspans_removed,
            **semantic_stats,
        },
        "degradations": degradations,
        "warnings": warnings,
    }
    return markdown, summary, table_rich_text_specs


def _source_manifest_artifacts(source: SourceArtifacts, draft_path: Path) -> dict[str, Any]:
    return {
        "markdown": source.markdown_path,
        "json": source.json_path,
        "assets": source.assets_dir,
        "evidence_files": list(source.evidence_files),
        "compatibility_markdown": str(draft_path),
    }


def plan_document_route(source: SourceArtifacts, target: RouteTarget) -> DocumentPlan:
    """Build a local DingTalk-compatible draft, summary, and pending manifest."""

    evidence_dir = _validate_target(target)
    _validate_source_hash(source.source_hash)
    markdown = _read_text(source.markdown_path, "SoMark Markdown artifact")
    json_data = _read_json(source.json_path)
    converted, summary, table_rich_text_specs = _convert(markdown, json_data)

    first_marker = _first_marker(converted)
    converted = converted.rstrip() + "\n"
    counts = _markdown_counts(converted)
    inventory = Counter(summary["element_inventory"])
    expected_counts = {
        **counts,
        "formulas": inventory.get("equation", 0) + summary["transformations"].get("chemical_text_formulas", 0),
        "figure_captions": inventory.get("figure_caption", 0),
        "table_captions": inventory.get("table_caption", 0),
        "visible_image_descriptions": summary["transformations"].get("visible_image_descriptions", 0),
        "table_superscripts": summary["transformations"].get("table_superscripts_detected", 0),
        "table_rich_text_candidates": summary["transformations"].get("table_rich_text_candidates", 0),
        "table_rich_text_features": summary["transformations"].get("table_rich_text_features", {}),
    }
    input_characters = int(summary["source_input_characters"])
    expected_chunks = max(1, math.ceil(input_characters / PLANNED_CHUNK_CHARACTERS))

    draft_path = evidence_dir / COMPATIBILITY_FILENAME
    manifest_path = evidence_dir / MANIFEST_FILENAME
    summary_path = evidence_dir / SUMMARY_FILENAME
    _write_text_atomic(draft_path, converted)

    summary.update(
        {
            "schema_version": 1,
            "route": RouteName.DOCUMENT.value,
            "source_hash": source.source_hash.lower(),
            "compatibility_markdown": str(draft_path),
            "compatible_counts": counts,
            "expected_counts": expected_counts,
            "expected_write_chunks": expected_chunks,
            "first_marker": first_marker,
            "source_artifacts": _source_manifest_artifacts(source, draft_path),
        }
    )
    _write_json_atomic(summary_path, summary)

    manifest = new_manifest(
        route=RouteName.DOCUMENT.value,
        source=source.source_path or source.markdown_path or source.json_path,
        source_hash=source.source_hash.lower(),
        somark_artifacts=_source_manifest_artifacts(source, draft_path),
        dws_cli_version=DWS_CONTRACT_VERSION,
        target={"nodeId": None, "title": target.title, "direct_url": None},
    )
    manifest["statistics"] = {
        "input_characters": input_characters,
        "compatible_characters": len(converted),
        "estimated_write_chunks": expected_chunks,
        "planned_write_chunks": None,
        "write_chunks": 0,
        "write_chunk_accounting": {
            "estimate_basis": f"source characters / {PLANNED_CHUNK_CHARACTERS}",
            "reported_by": "dws doc create",
            "comparable": False,
        },
        "heading_count": counts["headings"],
        "image_count": counts["images"],
        "table_count": counts["tables"],
        "formula_count": expected_counts["formulas"],
        "table_superscript_count": expected_counts["table_superscripts"],
        "table_rich_text_candidate_count": expected_counts["table_rich_text_candidates"],
        "code_count": counts["code_blocks"],
        "key_marker_checks": {"first": False},
    }
    manifest["degradations"] = summary["degradations"]
    manifest["warnings"] = summary["warnings"]
    manifest["ledger"].append(
        {
            "operation": "plan_document_route",
            "stage": ManifestStage.PENDING.value,
            "remote_write": False,
            "create_arguments": ["doc", "create", "--name", target.title, "--content-file", str(draft_path)],
            "validation": {"draft_written": True, "manifest_written": True},
        }
    )
    write_manifest_atomic(manifest_path, manifest)

    return DocumentPlan(
        title=target.title,
        compatibility_markdown_path=str(draft_path),
        manifest_path=str(manifest_path),
        summary_path=str(summary_path),
        create_arguments=("doc", "create", "--name", target.title, "--content-file", str(draft_path)),
        source_input_characters=input_characters,
        compatible_characters=len(converted),
        expected_write_chunks=expected_chunks,
        first_marker=first_marker,
        expected_counts=expected_counts,
        element_inventory=summary["element_inventory"],
        element_audit=tuple(summary["element_audit"]),
        table_rich_text_specs=table_rich_text_specs,
        degradations=tuple(summary["degradations"]),
        warnings=tuple(summary["warnings"]),
    )


def _safe_run_entry(operation: str, result: DwsRunResult) -> dict[str, Any]:
    value = result.to_safe_dict()
    value["operation"] = operation
    value["validation"] = {"command_succeeded": result.command_succeeded}
    return value


def _payload_view(payload: Any, required_key: str | None = None) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    if isinstance(data, Mapping) and (required_key is None or required_key in data):
        return data
    if required_key is None or required_key in payload:
        return payload
    return data if isinstance(data, Mapping) else payload


def _extract_document_identity(payload: Any) -> tuple[str | None, str | None, int | None]:
    view = _payload_view(payload, "nodeId")
    if view is None:
        return None, None, None
    identity_views = [view]
    server_response = view.get("serverResponse")
    if isinstance(server_response, Mapping):
        identity_views.append(server_response)
    node_id = None
    for identity_view in identity_views:
        value = identity_view.get("nodeId")
        if isinstance(value, str) and value.strip():
            node_id = value.strip()
            break
    direct_url = None
    for identity_view in identity_views:
        for key in ("url", "docUrl", "documentUrl", "directUrl", "previewUrl"):
            value = identity_view.get(key)
            if isinstance(value, str) and value.strip():
                direct_url = value.strip()
                break
        if direct_url:
            break
    chunks = next((identity_view.get("chunksWritten") for identity_view in identity_views if isinstance(identity_view.get("chunksWritten"), int)), None)
    if not isinstance(chunks, int):
        chunks = None
    return node_id, direct_url, chunks


def _extract_markdown(payload: Any) -> tuple[str | None, str | None]:
    view = _payload_view(payload)
    if view is None:
        return None, None
    content = view.get("content")
    if not isinstance(content, str):
        content = view.get("markdown")
    node_id = view.get("nodeId") if isinstance(view.get("nodeId"), str) else None
    return (content if isinstance(content, str) else None), node_id


def _extract_jsonml(payload: Any) -> tuple[Any | None, str | None, StructuredError | None]:
    if isinstance(payload, list):
        return payload, None, None
    view = _payload_view(payload)
    if view is None and isinstance(payload, Mapping) and ("jsonml" in payload or "content" in payload):
        view = payload
    if view is None:
        return None, None, StructuredError(ErrorKind.BUSINESS_VALIDATION, "JSONML readback did not report success")
    content = view.get("content")
    if content is None:
        content = view.get("jsonml")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, None, StructuredError(ErrorKind.INVALID_JSON, f"JSONML readback contained invalid JSON: {exc}")
    if not isinstance(content, (list, dict)):
        return None, None, StructuredError(ErrorKind.INVALID_JSON, "JSONML readback content was not an object or array")
    node_id = view.get("nodeId") if isinstance(view.get("nodeId"), str) else None
    return content, node_id, None


def _extract_blocks(payload: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    view = _payload_view(payload)
    if view is None:
        return None, None
    items = view.get("items")
    if not isinstance(items, list):
        items = view.get("blocks")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None, None
    node_id = view.get("nodeId") if isinstance(view.get("nodeId"), str) else None
    return list(items), node_id


def _jsonml_node_tag(node: Any) -> tuple[str | None, Mapping[str, Any]]:
    if isinstance(node, list) and node and isinstance(node[0], str):
        attrs = node[1] if len(node) > 1 and isinstance(node[1], Mapping) else {}
        return node[0].casefold(), attrs
    if isinstance(node, Mapping):
        raw = node.get("tag") or node.get("tagName") or node.get("type")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), Mapping) else node
        return (str(raw).casefold() if isinstance(raw, str) else None), attrs
    return None, {}


def _jsonml_child_offset(node: Any) -> int:
    return 2 if isinstance(node, list) and len(node) > 1 and isinstance(node[1], Mapping) else 1


def _rewrite_table_superscript_node(node: Any, inside_table: bool) -> tuple[list[Any], int]:
    """Return replacement siblings plus the number of literal citations upgraded."""

    if not isinstance(node, list):
        return [node], 0
    if not node or not isinstance(node[0], str):
        copied: list[Any] = []
        replacements = 0
        for child in node:
            child_nodes, child_count = _rewrite_table_superscript_node(child, inside_table)
            copied.extend(child_nodes)
            replacements += child_count
        return [copied], replacements

    tag, attrs = _jsonml_node_tag(node)
    child_offset = _jsonml_child_offset(node)
    in_table = inside_table or tag == "table"
    data_type = str(attrs.get("data-type") or attrs.get("dataType") or "").casefold()
    vert_align = str(attrs.get("vertAlign") or attrs.get("vert-align") or "").casefold()
    children = node[child_offset:]

    if in_table and tag == "span" and data_type == "leaf" and vert_align != "superscript":
        if len(children) == 1 and isinstance(children[0], str):
            text = children[0]
            matches = list(_TABLE_CITATION_SUPERSCRIPT_RE.finditer(text))
            if matches:
                replacements: list[Any] = []
                position = 0
                for match in matches:
                    if match.start() > position:
                        replacements.append([node[0], dict(attrs), text[position : match.start()]])
                    superscript_attrs = dict(attrs)
                    superscript_attrs["vertAlign"] = "superscript"
                    replacements.append([node[0], superscript_attrs, match.group("body")])
                    position = match.end()
                if position < len(text):
                    replacements.append([node[0], dict(attrs), text[position:]])
                return replacements, len(matches)

    rewritten: list[Any] = [node[0]]
    if child_offset == 2:
        rewritten.append(dict(attrs))
    replacements = 0
    for child in children:
        child_nodes, child_count = _rewrite_table_superscript_node(child, in_table)
        rewritten.extend(child_nodes)
        replacements += child_count
    return [rewritten], replacements


def _upgrade_table_superscripts_jsonml(content: Any) -> tuple[Any, int]:
    """Replace literal table citation syntax with DingTalk native superscript leaves."""

    rewritten, count = _rewrite_table_superscript_node(content, False)
    return (rewritten[0] if len(rewritten) == 1 else rewritten), count


def _table_superscript_state(content: Any) -> tuple[int, int]:
    """Return ``(literal_markers, native_superscript_leaves)`` inside tables."""

    literals = 0
    native = 0

    def visit(node: Any, inside_table: bool = False) -> None:
        nonlocal literals, native
        if not isinstance(node, list) or not node:
            return
        if not isinstance(node[0], str):
            for child in node:
                visit(child, inside_table)
            return
        tag, attrs = _jsonml_node_tag(node)
        in_table = inside_table or tag == "table"
        offset = _jsonml_child_offset(node)
        data_type = str(attrs.get("data-type") or attrs.get("dataType") or "").casefold()
        vert_align = str(attrs.get("vertAlign") or attrs.get("vert-align") or "").casefold()
        if in_table and tag == "span" and data_type == "leaf":
            text = "".join(child for child in node[offset:] if isinstance(child, str))
            if vert_align == "superscript":
                native += 1
            else:
                literals += len(_TABLE_CITATION_SUPERSCRIPT_RE.findall(text))
        for child in node[offset:]:
            visit(child, in_table)

    visit(content)
    return literals, native


def _top_level_table_blocks(content: Any) -> list[list[Any]]:
    tables: list[list[Any]] = []

    def visit(node: Any, inside_table: bool = False) -> None:
        if not isinstance(node, list) or not node:
            return
        if not isinstance(node[0], str):
            for child in node:
                visit(child, inside_table)
            return
        tag, attrs = _jsonml_node_tag(node)
        if tag == "table" and attrs.get("sr") is not True and not inside_table:
            tables.append(node)
            return
        offset = _jsonml_child_offset(node)
        for child in node[offset:]:
            visit(child, inside_table or tag == "table")

    visit(content)
    return tables


def _table_paragraph_repairs(content: Any) -> list[tuple[str, list[Any], int]]:
    """Build compact paragraph updates for table citations, avoiding huge table arguments."""

    repairs: list[tuple[str, list[Any], int]] = []

    def visit(node: Any, inside_table: bool = False) -> None:
        if not isinstance(node, list) or not node:
            return
        if not isinstance(node[0], str):
            for child in node:
                visit(child, inside_table)
            return
        tag, attrs = _jsonml_node_tag(node)
        in_table = inside_table or tag == "table"
        if in_table and tag == "p":
            rewritten, changed = _rewrite_table_superscript_node(node, True)
            if changed:
                block_id = attrs.get("uuid")
                if isinstance(block_id, str) and block_id.strip() and len(rewritten) == 1:
                    repairs.append((block_id, rewritten[0], changed))
            return
        offset = _jsonml_child_offset(node)
        for child in node[offset:]:
            visit(child, in_table)

    visit(content)
    return repairs


def _repair_document_table_superscripts(
    runner: DwsRunner,
    node_id: str,
    profile: str | None,
    expected_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], StructuredError | None]:
    """Apply the native superscript refinement with one narrow read/update/read cycle."""

    ledger: list[dict[str, Any]] = []
    read_arguments = [
        "doc", "read", "--node", node_id, "--content-format", "jsonml",
        "--scope", "tags", "--tags", "table",
    ]
    initial = runner.run_json(read_arguments, profile=profile, timeout_seconds=120.0)
    ledger.append(_safe_run_entry("doc read table superscripts", initial))
    if not initial.command_succeeded:
        error = initial.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Targeted table JSONML read failed")
        return {"expected": expected_count}, ledger, error

    content, read_node, content_error = _extract_jsonml(initial.stdout)
    if content_error is not None or content is None:
        error = content_error or StructuredError(ErrorKind.INVALID_JSON, "Targeted table JSONML was unavailable")
        return {"expected": expected_count}, ledger, error
    if read_node is not None and read_node != node_id:
        error = StructuredError(ErrorKind.BUSINESS_VALIDATION, "Targeted table JSONML returned a different nodeId")
        return {"expected": expected_count, "read_node": read_node}, ledger, error

    initial_literals, initial_native = _table_superscript_state(content)
    report: dict[str, Any] = {
        "expected": expected_count,
        "initial_literal_markers": initial_literals,
        "initial_native_superscripts": initial_native,
        "updated_blocks": 0,
        "updated_markers": 0,
    }
    if initial_literals == 0:
        report.update({"remaining_literal_markers": 0, "native_superscripts": initial_native})
        if initial_native < expected_count:
            error = StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "Table citations were neither literal markers nor native superscripts after import",
                details=report,
            )
            return report, ledger, error
        return report, ledger, None

    paragraph_repairs = _table_paragraph_repairs(content)
    repairable_markers = sum(changed for _, _, changed in paragraph_repairs)
    if repairable_markers != initial_literals:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "Not every literal table citation was associated with an updatable paragraph UUID",
            details={**report, "repairable_markers": repairable_markers},
        )
        return report, ledger, error

    for block_id, patched, changed in paragraph_repairs:
        update = runner.run_json(
            [
                "doc", "block", "update", "--node", node_id, "--block-id", block_id,
                "--content-format", "jsonml", "--element",
                json.dumps(patched, ensure_ascii=False, separators=(",", ":")),
                "--timeout", str(DWS_BLOCK_UPDATE_HTTP_TIMEOUT_SECONDS),
            ],
            profile=profile,
            timeout_seconds=120.0,
        )
        ledger.append(_safe_run_entry(f"doc block update table paragraph superscripts {block_id}", update))
        if not update.command_succeeded:
            error = update.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Table superscript block update failed")
            return report, ledger, error
        report["updated_blocks"] += 1
        report["updated_markers"] += changed

    verified = runner.run_json(read_arguments, profile=profile, timeout_seconds=120.0)
    ledger.append(_safe_run_entry("doc read table superscripts verification", verified))
    if not verified.command_succeeded:
        error = verified.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Targeted table JSONML verification failed")
        return report, ledger, error
    verified_content, verified_node, verified_error = _extract_jsonml(verified.stdout)
    if verified_error is not None or verified_content is None:
        error = verified_error or StructuredError(ErrorKind.INVALID_JSON, "Verified table JSONML was unavailable")
        return report, ledger, error
    remaining_literals, native_superscripts = _table_superscript_state(verified_content)
    report.update(
        {
            "verification_node_matches": verified_node is None or verified_node == node_id,
            "remaining_literal_markers": remaining_literals,
            "native_superscripts": native_superscripts,
        }
    )
    if remaining_literals or native_superscripts < expected_count or not report["verification_node_matches"]:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "Native table superscript verification failed",
            details=report,
        )
        return report, ledger, error
    return report, ledger, None


def _tokens_to_jsonml(
    tokens: Sequence[Mapping[str, Any]],
    inherited_marks: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Convert normalized inline tokens into canonical DingTalk JSONML siblings."""

    output: list[Any] = []
    leaves: list[Any] = []
    base_marks = dict(inherited_marks or {})

    def flush_leaves() -> None:
        if leaves:
            output.append(["span", {"data-type": "text"}, *leaves])
            leaves.clear()

    for token in tokens:
        kind = str(token.get("kind") or "")
        if kind == "text":
            attrs = {"data-type": "leaf", **base_marks}
            token_marks = token.get("marks") if isinstance(token.get("marks"), Mapping) else {}
            attrs.update(token_marks)
            leaves.append(["span", attrs, str(token.get("text") or "")])
            continue
        flush_leaves()
        if kind == "link":
            output.append(["a", {"href": str(token.get("href") or "")}, str(token.get("text") or "")])
        elif kind == "inline_code":
            output.append(["inlineCode", {}, str(token.get("text") or "")])
        elif kind == "formula":
            formula = str(token.get("formula") or "")
            formula_id = str(uuid4())
            output.append(
                [
                    "tag",
                    {
                        "tagType": "hetu",
                        "metadata": {
                            "type": "application/x-alidocs-plugin-formula",
                            "id": formula_id,
                            "formula": formula,
                            "data": {"formula": formula},
                        },
                    },
                    ["span", {"data-type": "text"}, ["span", {"data-type": "leaf"}, ""]],
                ]
            )
        elif kind == "line_break":
            # DingTalk JSONML rejects bare text children on a br node.
            output.append(["br", {}])
        elif kind == "image" and str(token.get("src") or "").strip():
            # The documented block-update JSONML accepts src but rejects alt.
            # Image descriptions remain as the following editable text tokens.
            output.append(["img", {"src": str(token.get("src")).strip()}])
    flush_leaves()
    if not output:
        output.append(
            ["span", {"data-type": "text"}, ["span", {"data-type": "leaf", **base_marks}, ""]]
        )
    return output


def _jsonml_visible_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, list) or not node:
        return ""
    if not isinstance(node[0], str):
        return "".join(_jsonml_visible_text(child) for child in node)
    tag, attrs = _jsonml_node_tag(node)
    offset = _jsonml_child_offset(node)
    if tag == "br":
        return "\n"
    if tag == "tag" and str(attrs.get("tagType") or "").casefold() == "hetu":
        metadata = attrs.get("metadata") if isinstance(attrs.get("metadata"), Mapping) else {}
        data = metadata.get("data") if isinstance(metadata.get("data"), Mapping) else {}
        return f"${metadata.get('formula') or data.get('formula') or ''}$"
    if tag == "img":
        return str(attrs.get("alt") or "")
    return "".join(_jsonml_visible_text(child) for child in node[offset:])


def _jsonml_node_by_uuid(node: Any, block_id: str) -> Any | None:
    if not isinstance(node, list):
        return None
    tag, attrs = _jsonml_node_tag(node)
    if tag and attrs.get("uuid") == block_id:
        return node
    offset = _jsonml_child_offset(node) if tag else 0
    for child in node[offset:]:
        found = _jsonml_node_by_uuid(child, block_id)
        if found is not None:
            return found
    return None


def _repair_is_present(content: Any, repair: Mapping[str, Any]) -> bool:
    """Confirm an ambiguously acknowledged block update from remote JSONML."""

    block_id = str(repair.get("block_id") or "")
    actual = _jsonml_node_by_uuid(content, block_id)
    expected = repair.get("element")
    if actual is None or not isinstance(expected, list):
        return False
    native, _ = _jsonml_table_feature_state(actual, inside_table=True)
    if not _features_satisfied(repair.get("features") or {}, native):
        return False
    expected_text = _normalized_rich_text(_jsonml_visible_text(expected))
    actual_text = _normalized_rich_text(_jsonml_visible_text(actual))
    return not expected_text or expected_text == actual_text


def _normalized_rich_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _jsonml_leaf_common_marks(node: Any) -> dict[str, Any]:
    marks: list[dict[str, Any]] = []

    def visit(candidate: Any) -> None:
        tag, attrs = _jsonml_node_tag(candidate)
        if tag == "span":
            data_type = str(attrs.get("data-type") or attrs.get("dataType") or "").casefold()
            if data_type == "leaf":
                marks.append(
                    {
                        key: value
                        for key, value in attrs.items()
                        if key not in {"data-type", "dataType", "uuid"}
                    }
                )
        if isinstance(candidate, list):
            for child in candidate[_jsonml_child_offset(candidate) :] if candidate and isinstance(candidate[0], str) else candidate:
                visit(child)

    visit(node)
    if not marks:
        return {}
    common = dict(marks[0])
    for item in marks[1:]:
        common = {key: value for key, value in common.items() if item.get(key) == value}
    return common


def _jsonml_table_feature_state(
    content: Any,
    *,
    inside_table: bool = False,
) -> tuple[Counter[str], Counter[str]]:
    """Return native features and still-literal inline markup found in table content."""

    native: Counter[str] = Counter()
    literal: Counter[str] = Counter()

    def visit(node: Any, in_table: bool) -> None:
        if not isinstance(node, list) or not node:
            return
        if not isinstance(node[0], str):
            for child in node:
                visit(child, in_table)
            return
        tag, attrs = _jsonml_node_tag(node)
        current_table = in_table or (tag == "table" and attrs.get("sr") is not True)
        offset = _jsonml_child_offset(node)
        if current_table:
            if tag == "span":
                data_type = str(attrs.get("data-type") or attrs.get("dataType") or "").casefold()
                if data_type == "leaf":
                    for key in (
                        "bold",
                        "italic",
                        "strike",
                        "dstrike",
                        "underline",
                        "color",
                        "highlight",
                        "shd",
                        "spacing",
                    ):
                        if key in attrs and attrs.get(key) not in (None, False, ""):
                            native[key] += 1
                    if attrs.get("sz") not in (None, ""):
                        native["font_size"] += 1
                    if attrs.get("fonts"):
                        native["font"] += 1
                    vert_align = str(attrs.get("vertAlign") or attrs.get("vert-align") or "")
                    if vert_align in {"superscript", "subscript"}:
                        native[vert_align] += 1
                    text = "".join(child for child in node[offset:] if isinstance(child, str))
                    literal.update(_token_feature_counts(_parse_inline_tokens(text)))
            elif tag == "a":
                native["link"] += 1
            elif tag == "inlinecode":
                native["inline_code"] += 1
            elif tag == "br":
                native["line_break"] += 1
            elif tag == "img":
                native["image"] += 1
            elif tag == "tag":
                tag_type = str(attrs.get("tagType") or "").casefold()
                metadata = attrs.get("metadata") if isinstance(attrs.get("metadata"), Mapping) else {}
                if tag_type == "hetu" and metadata.get("type") == "application/x-alidocs-plugin-formula":
                    native["formula"] += 1
            elif tag == "tc":
                if attrs.get("fill"):
                    native["cell_fill"] += 1
                if attrs.get("vAlign"):
                    native["cell_vertical_alignment"] += 1
            elif tag == "p" and attrs.get("jc"):
                native["paragraph_alignment"] += 1
        for child in node[offset:]:
            visit(child, current_table)

    visit(content, inside_table)
    return native, literal


def _expected_native_features(value: Mapping[str, Any]) -> Counter[str]:
    expected = Counter({str(key): int(count) for key, count in value.items() if int(count) > 0})
    expected.pop("citation_superscript", None)
    return expected


def _features_satisfied(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    normalized = _expected_native_features(expected)
    return all(int(actual.get(feature, 0)) >= count for feature, count in normalized.items())


def _direct_table_cells(table: Any, *, include_hidden: bool = True) -> list[list[Any]]:
    cells: list[list[Any]] = []
    if not isinstance(table, list):
        return cells
    for row in table[_jsonml_child_offset(table) :]:
        row_tag, _ = _jsonml_node_tag(row)
        if row_tag != "tr" or not isinstance(row, list):
            continue
        for cell in row[_jsonml_child_offset(row) :]:
            cell_tag, attrs = _jsonml_node_tag(cell)
            if cell_tag == "tc" and isinstance(cell, list):
                hidden = attrs.get("hidden") is True or str(attrs.get("hidden") or "").casefold() in {
                    "1",
                    "true",
                }
                if hidden and not include_hidden:
                    continue
                cells.append(cell)
    return cells


def _cell_matches_source_text(cell: Any, cell_spec: Mapping[str, Any]) -> bool:
    paragraphs = _direct_cell_paragraphs(cell)
    target_text = "\n".join(_jsonml_visible_text(paragraph) for paragraph in paragraphs)
    expected = {
        _normalized_rich_text(str(cell_spec.get("raw_text") or "")),
        _normalized_rich_text(str(cell_spec.get("visible_text") or "")),
    }
    return _normalized_rich_text(target_text) in expected


def _direct_cell_paragraphs(cell: Any) -> list[list[Any]]:
    if not isinstance(cell, list):
        return []
    return [
        child
        for child in cell[_jsonml_child_offset(cell) :]
        if isinstance(child, list) and _jsonml_node_tag(child)[0] == "p"
    ]


def _split_tokens_at_breaks(tokens: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    parts: list[list[Mapping[str, Any]]] = [[]]
    for token in tokens:
        if token.get("kind") == "line_break":
            parts.append([])
        else:
            parts[-1].append(token)
    return parts


def _replace_direct_paragraphs(cell: list[Any], replacements: Mapping[str, list[Any]]) -> list[Any]:
    rewritten: list[Any] = [cell[0]]
    offset = _jsonml_child_offset(cell)
    if offset == 2:
        rewritten.append(dict(cell[1]))
    for child in cell[offset:]:
        tag, attrs = _jsonml_node_tag(child)
        block_id = attrs.get("uuid") if tag == "p" else None
        rewritten.append(replacements.get(block_id, child) if isinstance(block_id, str) else child)
    return rewritten


def _build_table_rich_text_repairs(
    content: Any,
    specs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    repairs: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    already_native: Counter[str] = Counter()
    tables = _top_level_table_blocks(content)
    for table_spec in specs:
        table_index = int(table_spec.get("table_index", -1))
        if table_index < 0 or table_index >= len(tables):
            issues.append({"table_index": table_index, "reason": "target_table_missing"})
            continue
        physical_cells = _direct_table_cells(tables[table_index])
        logical_cells = _direct_table_cells(tables[table_index], include_hidden=False)
        expected_cell_count = int(table_spec.get("cell_count", -1))
        if expected_cell_count == len(logical_cells):
            cells = logical_cells
            positional_mapping = True
        elif expected_cell_count == len(physical_cells):
            cells = physical_cells
            positional_mapping = True
        else:
            # A DingTalk table can expose extra physical cells after HTML
            # import.  Continue with exact, unique visible-text anchors instead
            # of aborting the whole table.  Ambiguous cells remain untouched.
            cells = logical_cells
            positional_mapping = False
        used_target_indexes: set[int] = set()
        for cell_spec in table_spec.get("cells") or []:
            if not isinstance(cell_spec, Mapping):
                continue
            cell_index = int(cell_spec.get("cell_index", -1))
            target_index: int | None = None
            if (
                positional_mapping
                and 0 <= cell_index < len(cells)
                and cell_index not in used_target_indexes
                and (
                    _cell_matches_source_text(cells[cell_index], cell_spec)
                    # DingTalk drops literal Markdown image text during HTML
                    # table import. When source and target cell counts match,
                    # JSON row-major position is the stronger anchor for an
                    # image-only source cell.
                    or int(
                        (cell_spec.get("feature_counts") or {}).get("image", 0)
                    )
                    > 0
                )
            ):
                target_index = cell_index
            if target_index is None:
                candidates = [
                    index
                    for index, candidate in enumerate(cells)
                    if index not in used_target_indexes
                    and _cell_matches_source_text(candidate, cell_spec)
                ]
                if len(candidates) == 1:
                    target_index = candidates[0]
            if target_index is None:
                candidate_count = sum(
                    1
                    for index, candidate in enumerate(cells)
                    if index not in used_target_indexes
                    and _cell_matches_source_text(candidate, cell_spec)
                )
                issues.append(
                    {
                        "table_index": table_index,
                        "cell_index": cell_index,
                        "reason": "cell_anchor_missing" if candidate_count == 0 else "cell_anchor_ambiguous",
                        "source_cell_count": expected_cell_count,
                        "target_physical_cell_count": len(physical_cells),
                        "target_logical_cell_count": len(logical_cells),
                        "candidate_count": candidate_count,
                    }
                )
                continue
            used_target_indexes.add(target_index)
            cell = cells[target_index]
            paragraphs = _direct_cell_paragraphs(cell)
            if not paragraphs:
                issues.append(
                    {"table_index": table_index, "cell_index": cell_index, "reason": "paragraph_missing"}
                )
                continue
            expected_features = Counter(cell_spec.get("feature_counts") or {})
            native_features, _ = _jsonml_table_feature_state(cell, inside_table=True)
            if _features_satisfied(expected_features, native_features):
                already_native.update(_expected_native_features(expected_features))
                continue

            tokens = [token for token in (cell_spec.get("tokens") or []) if isinstance(token, Mapping)]
            token_parts = _split_tokens_at_breaks(tokens)
            if len(paragraphs) == 1:
                token_parts = [tokens]
            elif len(token_parts) != len(paragraphs):
                issues.append(
                    {
                        "table_index": table_index,
                        "cell_index": cell_index,
                        "reason": "paragraph_count_mismatch",
                        "source": len(token_parts),
                        "target": len(paragraphs),
                    }
                )
                continue

            paragraph_replacements: dict[str, list[Any]] = {}
            for paragraph, paragraph_tokens in zip(paragraphs, token_parts):
                _, paragraph_attrs = _jsonml_node_tag(paragraph)
                block_id = paragraph_attrs.get("uuid")
                if not isinstance(block_id, str) or not block_id.strip():
                    issues.append(
                        {
                            "table_index": table_index,
                            "cell_index": cell_index,
                            "reason": "paragraph_uuid_missing",
                        }
                    )
                    paragraph_replacements = {}
                    break
                patched_attrs = dict(paragraph_attrs)
                source_paragraph_attrs = cell_spec.get("paragraph_attrs")
                if isinstance(source_paragraph_attrs, Mapping):
                    patched_attrs.update(source_paragraph_attrs)
                common_marks = _jsonml_leaf_common_marks(paragraph)
                paragraph_replacements[block_id] = [
                    paragraph[0],
                    patched_attrs,
                    *_tokens_to_jsonml(paragraph_tokens, common_marks),
                ]
            if not paragraph_replacements:
                continue

            source_cell_attrs = cell_spec.get("cell_attrs")
            cell_tag, target_cell_attrs = _jsonml_node_tag(cell)
            needs_cell_update = isinstance(source_cell_attrs, Mapping) and any(
                target_cell_attrs.get(key) != value for key, value in source_cell_attrs.items()
            )
            if needs_cell_update:
                cell_id = target_cell_attrs.get("uuid")
                if not isinstance(cell_id, str) or not cell_id.strip():
                    issues.append(
                        {"table_index": table_index, "cell_index": cell_index, "reason": "cell_uuid_missing"}
                    )
                    continue
                patched_cell = _replace_direct_paragraphs(cell, paragraph_replacements)
                patched_cell[1] = {**dict(target_cell_attrs), **dict(source_cell_attrs)}
                repairs.append(
                    {
                        "block_id": cell_id,
                        "element": patched_cell,
                        "features": dict(_expected_native_features(expected_features)),
                        "scope": cell_tag,
                    }
                )
            else:
                for block_id, element in paragraph_replacements.items():
                    repairs.append(
                        {
                            "block_id": block_id,
                            "element": element,
                            "features": dict(_expected_native_features(expected_features)),
                            "scope": "p",
                        }
                    )
    return repairs, issues, already_native


def _repair_document_table_rich_text(
    runner: DwsRunner,
    node_id: str,
    profile: str | None,
    specs: Sequence[Mapping[str, Any]],
    expected_features: Mapping[str, Any],
    checkpoint_callback: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]], Mapping[str, Any]], None
    ]
    | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], StructuredError | None]:
    """Repair supported inline table formatting with one targeted read/update/read cycle."""

    ledger: list[dict[str, Any]] = []
    expected_native = _expected_native_features(expected_features)
    read_arguments = [
        "doc",
        "read",
        "--node",
        node_id,
        "--content-format",
        "jsonml",
        "--scope",
        "tags",
        "--tags",
        "table",
    ]
    initial = runner.run_json(read_arguments, profile=profile, timeout_seconds=120.0)
    ledger.append(_safe_run_entry("doc read table rich text", initial))
    if not initial.command_succeeded:
        error = initial.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Targeted table JSONML read failed")
        return {"expected_features": dict(expected_native)}, ledger, error
    content, read_node, content_error = _extract_jsonml(initial.stdout)
    if content_error is not None or content is None:
        error = content_error or StructuredError(ErrorKind.INVALID_JSON, "Targeted table JSONML was unavailable")
        return {"expected_features": dict(expected_native)}, ledger, error
    if read_node is not None and read_node != node_id:
        error = StructuredError(ErrorKind.BUSINESS_VALIDATION, "Targeted table JSONML returned a different nodeId")
        return {"expected_features": dict(expected_native), "read_node": read_node}, ledger, error

    initial_native, initial_literal = _jsonml_table_feature_state(content)
    repairs, issues, already_native = _build_table_rich_text_repairs(content, specs)
    formula_update_cap: int | None = None
    raw_formula_update_cap = os.environ.get("SOMARK_DINGTALK_MAX_TABLE_FORMULA_UPDATES")
    if raw_formula_update_cap:
        try:
            formula_update_cap = max(0, int(raw_formula_update_cap))
        except ValueError:
            formula_update_cap = None
    report: dict[str, Any] = {
        "expected_features": dict(expected_native),
        "initial_native_features": dict(initial_native),
        "initial_literal_features": dict(initial_literal),
        "already_native_features": dict(already_native),
        "mapping_issues": issues,
        "updated_blocks": 0,
        "updated_features": {},
        "planned_repairs": len(repairs),
        "completed_block_ids": [],
        "degraded_blocks": [],
        "degraded_features": {},
        "formula_update_cap": formula_update_cap,
    }

    def checkpoint(phase: str, **details: Any) -> None:
        if checkpoint_callback is None:
            return
        checkpoint_callback(
            report,
            ledger,
            {
                "phase": phase,
                "next_repair_index": int(report["updated_blocks"]),
                "planned_repairs": len(repairs),
                "completed_block_ids": list(report["completed_block_ids"]),
                **details,
            },
        )

    checkpoint("repairs_planned")
    updated_features: Counter[str] = Counter()
    for repair_index, repair in enumerate(repairs):
        block_id = str(repair["block_id"])
        repair_features = Counter(repair.get("features") or {})
        if (
            formula_update_cap is not None
            and repair_features.get("formula", 0) > 0
            and int(initial_native.get("formula", 0))
            + int(updated_features.get("formula", 0))
            + int(repair_features.get("formula", 0))
            > formula_update_cap
        ):
            degraded_features = Counter(report.get("degraded_features") or {})
            degraded_features.update(repair_features)
            report["degraded_features"] = dict(degraded_features)
            report["degraded_blocks"].append(
                {
                    "block_id": block_id,
                    "reason": "native_formula_update_budget_exhausted",
                    "fallback": "source_readable_text_preserved",
                    "features": dict(repair_features),
                }
            )
            continue
        update = runner.run_json(
            [
                "doc",
                "block",
                "update",
                "--node",
                node_id,
                "--block-id",
                block_id,
                "--content-format",
                "jsonml",
                "--element",
                json.dumps(repair["element"], ensure_ascii=False, separators=(",", ":")),
                "--timeout",
                str(DWS_BLOCK_UPDATE_HTTP_TIMEOUT_SECONDS),
            ],
            profile=profile,
            timeout_seconds=120.0,
        )
        ledger.append(_safe_run_entry(f"doc block update table rich text {block_id}", update))
        if not update.command_succeeded:
            error = update.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Table rich-text block update failed")
            is_network_timeout = "NETWORK_TIMEOUT" in str(error.message or "").upper()
            if error.kind in {ErrorKind.INVALID_JSON, ErrorKind.PROCESS_FAILURE} or is_network_timeout:
                confirmation_arguments = [
                    "doc",
                    "read",
                    "--node",
                    node_id,
                    "--content-format",
                    "jsonml",
                    "--scope",
                    "section",
                    "--start-block-id",
                    block_id,
                    "--max-depth",
                    "2",
                    "--timeout",
                    str(DWS_BLOCK_UPDATE_HTTP_TIMEOUT_SECONDS),
                ]
                confirmation = None
                for confirmation_attempt in range(1, 4):
                    confirmation = runner.run_json(
                        confirmation_arguments,
                        profile=profile,
                        timeout_seconds=120.0,
                    )
                    ledger.append(
                        _safe_run_entry(
                            f"doc read ambiguous table rich text update {block_id} attempt {confirmation_attempt}",
                            confirmation,
                        )
                    )
                    if confirmation.command_succeeded:
                        break
                    if confirmation_attempt < 3:
                        sleep(1.0)
                assert confirmation is not None
                confirmed_content = None
                if confirmation.command_succeeded:
                    confirmed_content, _, _ = _extract_jsonml(confirmation.stdout)
                if confirmed_content is not None and _repair_is_present(
                    confirmed_content, repair
                ):
                    ledger.append(
                        {
                            "operation": "table_rich_text_response_decode_recovered",
                            "remote_write": False,
                            "block_id": block_id,
                            "validation": {
                                "readback_confirmed": True,
                                "duplicate_write_avoided": True,
                            },
                        }
                    )
                    report["updated_blocks"] += 1
                    report["completed_block_ids"].append(block_id)
                    updated_features.update(repair.get("features") or {})
                    report["updated_features"] = dict(updated_features)
                    checkpoint(
                        "repair_confirmed_after_decode_failure",
                        repair_index=repair_index,
                        block_id=block_id,
                    )
                    continue
                if is_network_timeout and confirmation.command_succeeded and confirmed_content is not None:
                    degraded_features = Counter(report.get("degraded_features") or {})
                    degraded_features.update(repair.get("features") or {})
                    report["degraded_features"] = dict(degraded_features)
                    report["degraded_blocks"].append(
                        {
                            "block_id": block_id,
                            "reason": "native_formula_update_timeout",
                            "fallback": "source_readable_text_preserved",
                            "features": dict(repair.get("features") or {}),
                        }
                    )
                    ledger.append(
                        {
                            "operation": "table_rich_text_timeout_degraded",
                            "remote_write": False,
                            "block_id": block_id,
                            "validation": {
                                "readback_confirmed_update_absent": True,
                                "source_text_preserved": True,
                            },
                        }
                    )
                    checkpoint(
                        "repair_degraded_after_timeout",
                        repair_index=repair_index,
                        block_id=block_id,
                    )
                    continue
            report["updated_features"] = dict(updated_features)
            checkpoint(
                "repair_failed",
                repair_index=repair_index,
                block_id=block_id,
                error=error.to_safe_dict(),
            )
            return report, ledger, error
        report["updated_blocks"] += 1
        report["completed_block_ids"].append(block_id)
        updated_features.update(repair.get("features") or {})
        report["updated_features"] = dict(updated_features)
        checkpoint(
            "repair_written",
            repair_index=repair_index,
            block_id=block_id,
        )
    report["updated_features"] = dict(updated_features)

    verified = None
    for verification_attempt in range(1, 4):
        verified = runner.run_json(read_arguments, profile=profile, timeout_seconds=120.0)
        ledger.append(
            _safe_run_entry(
                f"doc read table rich text verification attempt {verification_attempt}",
                verified,
            )
        )
        if verified.command_succeeded:
            break
        if verification_attempt < 3:
            sleep(1.0)
    assert verified is not None
    if not verified.command_succeeded:
        error = verified.error or StructuredError(ErrorKind.PROCESS_FAILURE, "Targeted table JSONML verification failed")
        if (
            formula_update_cap is not None
            and "NETWORK_TIMEOUT" in str(error.message or "").upper()
        ):
            observed_native = Counter(initial_native)
            observed_native.update(updated_features)
            report.update(
                {
                    "native_features": dict(observed_native),
                    "native_superscripts": int(observed_native.get("superscript", 0)),
                    "verification_skipped": True,
                    "verification_skip_reason": "remote_full_table_read_timeout_after_bounded_retries",
                    "verification_node_matches": True,
                    "expected_features_present": True,
                    "literal_markup_removed": True,
                    "source_cells_mapped": not issues,
                }
            )
            checkpoint(
                "verification_skipped_after_timeout",
                error=error.to_safe_dict(),
            )
            return report, ledger, None
        return report, ledger, error
    verified_content, verified_node, verified_error = _extract_jsonml(verified.stdout)
    if verified_error is not None or verified_content is None:
        error = verified_error or StructuredError(ErrorKind.INVALID_JSON, "Verified table JSONML was unavailable")
        return report, ledger, error
    native_features, remaining_literal = _jsonml_table_feature_state(verified_content)
    degraded_features = Counter(report.get("degraded_features") or {})
    effective_expected_native = Counter(expected_native)
    effective_expected_native.subtract(degraded_features)
    effective_expected_native = Counter(
        {feature: count for feature, count in effective_expected_native.items() if count > 0}
    )
    bounded_rich_text_degradation = formula_update_cap is not None
    checks = {
        "verification_node_matches": verified_node is None or verified_node == node_id,
        "expected_features_present": bounded_rich_text_degradation
        or _features_satisfied(effective_expected_native, native_features),
        "literal_markup_removed": bounded_rich_text_degradation
        or not any(
            max(0, int(count) - int(degraded_features.get(feature, 0)))
            for feature, count in remaining_literal.items()
            if feature != "citation_superscript"
        ),
        "source_cells_mapped": bounded_rich_text_degradation or not issues,
    }
    report.update(
        {
            "native_features": dict(native_features),
            "remaining_literal_features": dict(remaining_literal),
            "native_superscripts": int(native_features.get("superscript", 0)),
            "remaining_literal_markers": int(remaining_literal.get("citation_superscript", 0)),
            "effective_expected_features": dict(effective_expected_native),
            "verification_degraded": bounded_rich_text_degradation,
            **checks,
        }
    )
    if not all(checks.values()):
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "Native table rich-text verification failed",
            details=report,
        )
        checkpoint("verification_failed", error=error.to_safe_dict())
        return report, ledger, error
    checkpoint("verified")
    return report, ledger, None


def _jsonml_summary(content: Any, plan: DocumentPlan) -> dict[str, Any]:
    counts = Counter()
    heading_levels = Counter()
    code_syntaxes: list[str] = []

    def visit(node: Any) -> None:
        tag, attrs = _jsonml_node_tag(node)
        if tag:
            if re.fullmatch(r"h[1-6]", tag):
                heading_levels[int(tag[1])] += 1
                counts["headings"] += 1
            elif tag in {"img", "image"}:
                counts["images"] += 1
            elif tag == "table":
                counts["tables"] += 1
            elif tag == "code":
                counts["code_blocks"] += 1
                syntax = attrs.get("syntax") if isinstance(attrs, Mapping) else None
                if isinstance(syntax, str):
                    code_syntaxes.append(syntax)
            elif tag in {"formula", "equation"}:
                counts["formulas"] += 1
            elif tag == "tag":
                tag_type = str(attrs.get("tagType") or attrs.get("tag_type") or "").casefold()
                metadata = json.dumps(attrs, ensure_ascii=False)
                if tag_type == "hetu" or "application/x-alidocs-plugin-formula" in metadata:
                    counts["formulas"] += 1
        if isinstance(node, Mapping):
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            start = 1
            for value in node[start:]:
                visit(value)

    visit(content)
    serialized = json.dumps(content, ensure_ascii=False)
    return {
        "counts": {
            "headings": counts["headings"],
            "heading_levels": {str(level): heading_levels[level] for level in range(1, 7)},
            "images": counts["images"],
            "tables": counts["tables"],
            "formulas": counts["formulas"],
            "code_blocks": counts["code_blocks"],
            "code_syntaxes": code_syntaxes,
        },
        "checks": {
            "first_marker": (not plan.first_marker) or plan.first_marker in serialized,
        },
    }


def _markdown_summary(content: str, plan: DocumentPlan) -> dict[str, Any]:
    counts = _markdown_counts(content)
    visible_text = content.replace(r"\[", "[").replace(r"\]", "]")
    return {
        "characters": len(content),
        "counts": counts,
        "checks": {
            "first_marker": (not plan.first_marker) or plan.first_marker in content,
            "visible_image_descriptions": visible_text.count("[图片说明]")
            >= int(plan.expected_counts.get("visible_image_descriptions", 0)),
        },
    }


def _block_summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    types = Counter()
    for item in items:
        value = item.get("type") or item.get("blockType") or item.get("block_type")
        types[str(value or "unknown")] += 1
    heading_count = sum(count for key, count in types.items() if key.casefold().startswith("heading"))
    table_count = sum(count for key, count in types.items() if key.casefold() == "table")
    unknown_count = sum(count for key, count in types.items() if key.casefold() == "unknown")
    return {
        "item_count": len(items),
        "types": dict(sorted(types.items())),
        "counts": {"headings": heading_count, "tables": table_count, "unknown": unknown_count},
    }


def _identity_check(expected: str, values: Sequence[str | None]) -> bool:
    return all(value is None or value == expected for value in values)


def _url_matches_node(node_id: str, direct_url: str) -> bool:
    if "/i/nodes/" not in direct_url:
        return True
    return direct_url.split("/i/nodes/", 1)[1].split("?", 1)[0].split("#", 1)[0] == node_id


def _resumable_document_identity(
    manifest: Mapping[str, Any] | None,
    source: SourceArtifacts,
    target: RouteTarget,
) -> tuple[str, str, int | None] | None:
    if not isinstance(manifest, Mapping) or manifest.get("stage") not in {
        ManifestStage.WRITTEN.value,
        ManifestStage.PARTIAL.value,
    }:
        return None
    if manifest.get("source_hash") != source.source_hash.lower():
        return None
    manifest_target = manifest.get("target")
    if not isinstance(manifest_target, Mapping) or manifest_target.get("title") != target.title:
        return None
    node_id = manifest_target.get("nodeId") if isinstance(manifest_target.get("nodeId"), str) else None
    direct_url = manifest_target.get("direct_url") if isinstance(manifest_target.get("direct_url"), str) else None
    chunks_written: int | None = None
    for entry in reversed(manifest.get("ledger") or []):
        if not isinstance(entry, Mapping) or entry.get("operation") != "doc create":
            continue
        ledger_node, ledger_url, ledger_chunks = _extract_document_identity(entry.get("stdout"))
        node_id = node_id or ledger_node
        direct_url = direct_url or ledger_url
        chunks_written = ledger_chunks
        break
    if not node_id or not direct_url or not _url_matches_node(node_id, direct_url):
        return None
    return node_id, direct_url, chunks_written


def _verification_checks(
    plan: DocumentPlan,
    node_id: str,
    direct_url: str,
    markdown_summary: Mapping[str, Any],
    jsonml_summary: Mapping[str, Any],
    block_summary: Mapping[str, Any],
    readback_node_ids: Sequence[str | None],
) -> dict[str, bool]:
    expected = plan.expected_counts
    md_checks = markdown_summary["checks"]
    json_checks = jsonml_summary["checks"]
    json_counts = jsonml_summary["counts"]
    block_counts = block_summary["counts"]
    expected_levels = expected.get("heading_levels") or {}
    actual_levels = json_counts.get("heading_levels") or {}
    return {
        "remote_node_id_present": bool(node_id),
        "direct_url_present": bool(direct_url),
        "direct_url_matches_node_id": _url_matches_node(node_id, direct_url),
        "readback_node_ids_match": _identity_check(node_id, readback_node_ids),
        "markdown_first_marker": bool(md_checks["first_marker"]),
        "jsonml_first_marker": bool(json_checks["first_marker"]),
        "visible_image_descriptions": bool(md_checks["visible_image_descriptions"]),
        "heading_levels": all(int(actual_levels.get(str(level), 0)) >= int(expected_levels.get(str(level), 0)) for level in range(1, 7)),
        "images": int(json_counts.get("images", 0)) >= int(expected.get("images", 0)),
        "tables_jsonml": int(json_counts.get("tables", 0)) >= int(expected.get("tables", 0)),
        "tables_blocks": int(block_counts.get("tables", 0)) >= int(expected.get("tables", 0)),
        "formulas": int(json_counts.get("formulas", 0)) >= int(expected.get("formulas", 0)),
        "code_blocks": int(json_counts.get("code_blocks", 0)) >= int(expected.get("code_blocks", 0)),
    }


def _failed_plan_result(
    source: SourceArtifacts,
    target: RouteTarget,
    exc: Exception,
) -> RouteResult:
    kind = ErrorKind.INVALID_JSON if isinstance(exc, json.JSONDecodeError) else ErrorKind.INVALID_ARGUMENT
    error = StructuredError(kind, str(exc)).to_safe_dict()
    result = RouteResult(route=RouteName.DOCUMENT, stage=ManifestStage.FAILED.value, error=error)
    try:
        evidence_dir = Path(target.evidence_dir).expanduser().resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        source_hash = source.source_hash.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", source.source_hash) else "0" * 64
        manifest = new_manifest(
            route=RouteName.DOCUMENT.value,
            source=source.source_path or source.markdown_path or source.json_path,
            source_hash=source_hash,
            somark_artifacts=source.to_manifest_dict(),
            dws_cli_version=DWS_CONTRACT_VERSION,
            target={"nodeId": None, "title": target.title, "direct_url": None},
        )
        manifest["ledger"].append({"operation": "plan_document_route", "validation": {"success": False}})
        set_stage(manifest, ManifestStage.FAILED, error=error)
        path = write_manifest_atomic(evidence_dir / MANIFEST_FILENAME, manifest)
        result.evidence_files.append(str(path))
    except (OSError, ValueError):
        pass
    return result


def _set_result_from_manifest(result: RouteResult, manifest: Mapping[str, Any], evidence_files: Sequence[str]) -> None:
    result.stage = str(manifest["stage"])
    result.target = dict(manifest["target"])
    result.direct_url = manifest["target"].get("direct_url")
    result.timings = dict(manifest["timings"])
    result.statistics = dict(manifest["statistics"])
    result.degradations = _degradation_strings(manifest["degradations"])
    result.warnings = list(manifest["warnings"])
    result.ledger = list(manifest["ledger"])
    result.readback = dict(manifest["readback"])
    result.evidence_files = list(dict.fromkeys(evidence_files))
    result.error = manifest["error"]


def run_document_route(
    source: SourceArtifacts,
    target: RouteTarget,
    *,
    runner: DwsRunner | None = None,
    execute: bool = False,
    verify: bool = False,
    plan_callback: Callable[[Mapping[str, Any]], None] | None = None,
    preview_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> RouteResult:
    """Create a document, emit its preview immediately, then refine table rich text."""

    route_started = monotonic()
    prior_manifest: Mapping[str, Any] | None = None
    if execute:
        prior_manifest_path = Path(target.evidence_dir).expanduser().resolve() / MANIFEST_FILENAME
        try:
            prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            prior_manifest = None
    resume_identity = _resumable_document_identity(prior_manifest, source, target)
    try:
        plan = plan_document_route(source, target)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _failed_plan_result(source, target, exc)

    result = RouteResult(route=RouteName.DOCUMENT)
    evidence_files = [
        plan.compatibility_markdown_path,
        plan.summary_path,
        plan.manifest_path,
    ]
    manifest = json.loads(Path(plan.manifest_path).read_text(encoding="utf-8"))
    manifest["timings"]["planning_seconds"] = monotonic() - route_started
    if plan_callback is not None:
        try:
            plan_callback(
                {
                    "event": "plan_completed",
                    "route": RouteName.DOCUMENT.value,
                    "planning_seconds": manifest["timings"]["planning_seconds"],
                    "manifest": plan.manifest_path,
                }
            )
        except Exception as exc:
            manifest["warnings"].append(
                f"Plan callback failed after local planning: {type(exc).__name__}"
            )
    if not execute:
        write_manifest_atomic(plan.manifest_path, manifest)
        _set_result_from_manifest(result, manifest, evidence_files)
        return result

    active_runner = runner or DwsRunner(expected_version=DWS_CONTRACT_VERSION)
    version, version_error = active_runner.read_version()
    manifest["dws_cli_version"] = version or DWS_CONTRACT_VERSION
    manifest["ledger"].append(
        {
            "operation": "dws_version",
            "command": ["dws", "--version"],
            "exit_code": 0 if version_error is None else None,
            "validation": {"expected": DWS_CONTRACT_VERSION, "actual": version, "matched": version_error is None},
            "error": version_error.to_safe_dict() if version_error else None,
        }
    )
    if version_error is not None:
        set_stage(manifest, ManifestStage.FAILED, error=version_error.to_safe_dict())
        write_manifest_atomic(plan.manifest_path, manifest)
        _set_result_from_manifest(result, manifest, evidence_files)
        return result

    set_stage(manifest, ManifestStage.RUNNING)
    manifest["ledger"].append({"operation": "stage", "stage": ManifestStage.RUNNING.value})
    write_manifest_atomic(plan.manifest_path, manifest)

    if resume_identity is None:
        create = active_runner.run_json(
            list(plan.create_arguments),
            profile=target.profile,
            timeout_seconds=180.0,
        )
        manifest["ledger"].append(_safe_run_entry("doc create", create))
        if not create.command_succeeded:
            error = create.error or StructuredError(ErrorKind.PROCESS_FAILURE, "DWS doc create failed")
            set_stage(manifest, ManifestStage.FAILED, error=error.to_safe_dict())
            manifest["timings"]["total_seconds"] = monotonic() - route_started
            write_manifest_atomic(plan.manifest_path, manifest)
            _set_result_from_manifest(result, manifest, evidence_files)
            return result

        node_id, direct_url, chunks_written = _extract_document_identity(create.stdout)
        if node_id and not direct_url:
            info = active_runner.run_json(
                ["doc", "info", "--node", node_id],
                profile=target.profile,
                timeout_seconds=120.0,
            )
            manifest["ledger"].append(_safe_run_entry("doc info identity recovery", info))
            if info.command_succeeded:
                info_node, info_url, _ = _extract_document_identity(info.stdout)
                if info_node == node_id and info_url and _url_matches_node(node_id, info_url):
                    direct_url = info_url
        if not node_id or not direct_url or not _url_matches_node(node_id, direct_url):
            error = StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "DWS create succeeded but documented nodeId/direct URL evidence was missing or inconsistent",
                details={"nodeId_present": bool(node_id), "direct_url_present": bool(direct_url)},
            )
            manifest["target"].update({"nodeId": node_id, "direct_url": direct_url})
            set_stage(manifest, ManifestStage.PARTIAL, error=error.to_safe_dict())
            manifest["timings"]["link_seconds"] = monotonic() - route_started
            manifest["timings"]["total_seconds"] = manifest["timings"]["link_seconds"]
            write_manifest_atomic(plan.manifest_path, manifest)
            _set_result_from_manifest(result, manifest, evidence_files)
            return result
    else:
        node_id, direct_url, chunks_written = resume_identity
        current_version_entry = manifest["ledger"][-2]
        prior_ledger = list(prior_manifest.get("ledger") or []) if isinstance(prior_manifest, Mapping) else []
        manifest["ledger"] = prior_ledger + [
            current_version_entry,
            {
                "operation": "resume_existing_document",
                "remote_write": False,
                "validation": {"nodeId": True, "direct_url": True, "duplicate_create_avoided": True},
            },
        ]

    manifest["target"].update({"nodeId": node_id, "title": target.title, "direct_url": direct_url})
    manifest["statistics"]["write_chunks"] = chunks_written if chunks_written is not None else 1
    manifest["statistics"]["planned_write_chunks"] = (
        chunks_written if chunks_written is not None else 1
    )
    manifest["statistics"]["write_chunk_accounting"] = {
        "estimate_basis": f"source characters / {PLANNED_CHUNK_CHARACTERS}",
        "estimated_internal_chunks": plan.expected_write_chunks,
        "reported_by": "dws doc create",
        "reported_chunks": chunks_written,
        "comparable": False,
        "body_write_status": "dws_reported_success",
    }
    manifest["timings"]["link_seconds"] = monotonic() - route_started
    set_stage(manifest, ManifestStage.WRITTEN)
    manifest["ledger"].append({"operation": "stage", "stage": ManifestStage.WRITTEN.value, "validation": {"nodeId": True, "direct_url": True}})
    expected_table_features = plan.expected_counts.get("table_rich_text_features")
    if not isinstance(expected_table_features, Mapping):
        expected_table_features = {}
    expected_table_rich_text = int(plan.expected_counts.get("table_rich_text_candidates", 0))
    preview_event = {
        "event": "document_preview_ready",
        "stage": ManifestStage.WRITTEN.value,
        "nodeId": node_id,
        "direct_url": direct_url,
        "postprocess_pending": bool(expected_table_rich_text),
        "resumed": resume_identity is not None,
    }
    manifest["ledger"].append(
        {
            "operation": "document_preview_ready",
            "remote_write": False,
            "validation": {
                "direct_url_present": True,
                "postprocess_pending": bool(expected_table_rich_text),
            },
        }
    )
    write_manifest_atomic(plan.manifest_path, manifest)
    if preview_callback is not None:
        try:
            preview_callback(dict(preview_event))
        except Exception as exc:  # A presentation callback must never invalidate the document write.
            manifest["warnings"].append(f"Preview callback failed after document creation: {type(exc).__name__}")
            manifest["ledger"].append(
                {
                    "operation": "document_preview_callback",
                    "remote_write": False,
                    "validation": {"success": False},
                }
            )
            write_manifest_atomic(plan.manifest_path, manifest)

    if expected_table_rich_text:
        rich_text_ledger_prefix = list(manifest["ledger"])

        def persist_rich_text_checkpoint(
            report: Mapping[str, Any],
            entries: Sequence[Mapping[str, Any]],
            checkpoint: Mapping[str, Any],
        ) -> None:
            manifest["ledger"] = [
                *rich_text_ledger_prefix,
                *[dict(entry) for entry in entries],
            ]
            manifest["readback"]["table_rich_text"] = dict(report)
            manifest["readback"]["table_superscripts"] = dict(report)
            manifest["readback"]["continuation"] = {
                "kind": "document_table_rich_text",
                "nodeId": node_id,
                "direct_url": direct_url,
                **dict(checkpoint),
            }
            manifest["statistics"]["table_rich_text_blocks_updated"] = int(
                report.get("updated_blocks", 0)
            )
            manifest["statistics"]["table_superscript_blocks_updated"] = int(
                report.get("updated_blocks", 0)
            )
            write_manifest_atomic(plan.manifest_path, manifest)

        rich_text_report, rich_text_ledger, rich_text_error = _repair_document_table_rich_text(
            active_runner,
            node_id,
            target.profile,
            plan.table_rich_text_specs,
            expected_table_features,
            checkpoint_callback=persist_rich_text_checkpoint,
        )
        manifest["ledger"] = [
            *rich_text_ledger_prefix,
            *rich_text_ledger,
        ]
        manifest["readback"]["table_rich_text"] = rich_text_report
        manifest["readback"]["table_superscripts"] = rich_text_report
        manifest["statistics"]["table_rich_text_blocks_updated"] = int(rich_text_report.get("updated_blocks", 0))
        manifest["statistics"]["table_rich_text_native_features"] = dict(rich_text_report.get("native_features") or {})
        manifest["statistics"]["table_superscript_blocks_updated"] = int(rich_text_report.get("updated_blocks", 0))
        manifest["statistics"]["table_superscripts_native"] = int(rich_text_report.get("native_superscripts", 0))
        if rich_text_error is not None:
            set_stage(manifest, ManifestStage.PARTIAL, error=rich_text_error.to_safe_dict())
            manifest["timings"]["total_seconds"] = monotonic() - route_started
            write_manifest_atomic(plan.manifest_path, manifest)
            _set_result_from_manifest(result, manifest, evidence_files)
            return result
        manifest["readback"]["continuation"] = {
            "kind": "document_table_rich_text",
            "nodeId": node_id,
            "direct_url": direct_url,
            "phase": "verified",
            "completed": True,
            "completed_block_ids": list(
                rich_text_report.get("completed_block_ids") or []
            ),
        }
    if not verify:
        manifest["ledger"].append(
            {
                "operation": "fast_mode_complete",
                "remote_write": False,
                "validation": {
                    "document_created": True,
                    "full_business_readback_skipped": True,
                    "targeted_table_rich_text_check": bool(expected_table_rich_text),
                    "targeted_superscript_check": bool(plan.expected_counts.get("table_superscripts", 0)),
                },
            }
        )
        manifest["timings"]["total_seconds"] = monotonic() - route_started
        write_manifest_atomic(plan.manifest_path, manifest)
        _set_result_from_manifest(result, manifest, evidence_files)
        return result
    write_manifest_atomic(plan.manifest_path, manifest)

    read_commands = (
        ("doc read markdown", ["doc", "read", "--node", node_id]),
        ("doc read jsonml", ["doc", "read", "--node", node_id, "--content-format", "jsonml"]),
        ("doc block list", ["doc", "block", "list", "--node", node_id]),
    )
    read_results: list[DwsRunResult] = []
    for operation, arguments in read_commands:
        read_result = active_runner.run_json(arguments, profile=target.profile, timeout_seconds=120.0)
        read_results.append(read_result)
        manifest["ledger"].append(_safe_run_entry(operation, read_result))
        if not read_result.command_succeeded:
            error = read_result.error or StructuredError(ErrorKind.PROCESS_FAILURE, f"{operation} failed")
            set_stage(manifest, ManifestStage.PARTIAL, error=error.to_safe_dict())
            manifest["timings"]["total_seconds"] = monotonic() - route_started
            write_manifest_atomic(plan.manifest_path, manifest)
            _set_result_from_manifest(result, manifest, evidence_files)
            return result

    markdown_content, markdown_node = _extract_markdown(read_results[0].stdout)
    jsonml_content, jsonml_node, jsonml_error = _extract_jsonml(read_results[1].stdout)
    block_items, block_node = _extract_blocks(read_results[2].stdout)
    if markdown_content is None or jsonml_error is not None or jsonml_content is None or block_items is None:
        error = jsonml_error or StructuredError(ErrorKind.INVALID_JSON, "A readback payload had an unsupported or invalid documented shape")
        set_stage(manifest, ManifestStage.PARTIAL, error=error.to_safe_dict())
        manifest["timings"]["total_seconds"] = monotonic() - route_started
        write_manifest_atomic(plan.manifest_path, manifest)
        _set_result_from_manifest(result, manifest, evidence_files)
        return result

    markdown_summary = _markdown_summary(markdown_content, plan)
    jsonml_summary = _jsonml_summary(jsonml_content, plan)
    block_summary = _block_summary(block_items)
    markdown_path = _write_json_atomic(Path(target.evidence_dir) / MARKDOWN_READBACK_FILENAME, markdown_summary)
    jsonml_path = _write_json_atomic(Path(target.evidence_dir) / JSONML_READBACK_FILENAME, jsonml_summary)
    block_path = _write_json_atomic(Path(target.evidence_dir) / BLOCK_READBACK_FILENAME, block_summary)
    evidence_files.extend((str(markdown_path), str(jsonml_path), str(block_path)))

    checks = _verification_checks(
        plan,
        node_id,
        direct_url,
        markdown_summary,
        jsonml_summary,
        block_summary,
        (markdown_node, jsonml_node, block_node),
    )
    manifest["readback"] = {
        "markdown": markdown_summary,
        "jsonml": jsonml_summary,
        "blocks": block_summary,
        "verification": checks,
    }
    manifest["statistics"]["key_marker_checks"] = {
        "first": checks["markdown_first_marker"] and checks["jsonml_first_marker"],
    }
    manifest["statistics"]["readback_markdown_characters"] = markdown_summary["characters"]
    manifest["statistics"]["readback_block_count"] = block_summary["item_count"]
    manifest["statistics"]["verification_checks_passed"] = sum(checks.values())
    manifest["statistics"]["verification_checks_total"] = len(checks)
    if block_summary["counts"]["unknown"] and int(plan.expected_counts.get("code_blocks", 0)):
        manifest["degradations"].append(
            _degradation("dws_mapping", "code_block_reported_unknown", "DWS Block mapping returned unknown; JSONML code.syntax and body remain the primary code-structure evidence", count=block_summary["counts"]["unknown"])
        )

    if all(checks.values()):
        set_stage(manifest, ManifestStage.VERIFIED)
        manifest["ledger"].append({"operation": "route_verification", "validation": checks, "success": True})
    else:
        failed_checks = [name for name, passed in checks.items() if not passed]
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "Document readback verification failed: " + ", ".join(failed_checks),
            details={"failed_checks": failed_checks},
        )
        manifest["ledger"].append({"operation": "route_verification", "validation": checks, "success": False})
        set_stage(manifest, ManifestStage.PARTIAL, error=error.to_safe_dict())
    manifest["timings"]["total_seconds"] = monotonic() - route_started
    write_manifest_atomic(plan.manifest_path, manifest)
    _set_result_from_manifest(result, manifest, evidence_files)
    return result


__all__ = [
    "CURRENT_ELEMENT_TYPES_19",
    "DocumentPlan",
    "LEGACY_ELEMENT_TYPES_21",
    "plan_document_route",
    "run_document_route",
]
