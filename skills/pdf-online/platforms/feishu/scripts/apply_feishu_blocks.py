#!/usr/bin/env python3
"""Apply native Feishu captions, formulas, and formula headings after import."""

from __future__ import annotations

import argparse
import copy
from collections import Counter
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DOCUMENT_RE = re.compile(r"(?:/docx/)?(?P<token>[A-Za-z0-9]{10,})")
TEXT_KEYS = (
    "text",
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "heading7",
    "heading8",
    "heading9",
    "bullet",
    "ordered",
    "code",
    "quote",
    "todo",
)
SUPPORTED_MANIFEST_VERSIONS = {"0.2.0", "0.2.1", "0.2.2"}
MAX_TABLE_IMAGE_BYTES = 20 * 1024 * 1024
DOCX_BATCH_UPDATE_SIZE = 20


def document_id(value: str) -> str:
    if "/docx/" in value:
        value = value.split("/docx/", 1)[1].split("?", 1)[0].split("#", 1)[0]
    match = DOCUMENT_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unable to read Feishu document token from: {value}")
    return match.group("token")


def _lark_command() -> list[str]:
    executable = shutil.which("lark-cli.cmd") or shutil.which("lark-cli")
    if not executable:
        raise RuntimeError("lark-cli was not found on PATH")
    path = Path(executable)
    if path.suffix.lower() == ".cmd":
        run_js = path.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        node = path.parent / "node.exe"
        node_executable = str(node) if node.is_file() else shutil.which("node")
        if run_js.is_file() and node_executable:
            return [str(node_executable), str(run_js)]
    return [str(path)]


def _decode_cli_json(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"(?m)^[ \t]*(?=[{\[])", output):
            candidate = output[match.end() :]
            try:
                payload, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, (dict, list)):
                return payload
        raise direct_error


def _run_lark(arguments: list[str], body: dict[str, Any] | None = None) -> Any:
    environment = os.environ.copy()
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    completed = subprocess.run(
        [*_lark_command(), *arguments],
        input=json.dumps(body, ensure_ascii=False) if body is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    try:
        payload = _decode_cli_json(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(output or "lark-cli returned no JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("lark-cli returned JSON with an unexpected shape")
    if completed.returncode != 0 or payload.get("ok") is False:
        error = payload.get("error") or payload
        raise RuntimeError(json.dumps(error, ensure_ascii=False))
    if payload.get("ok") is True:
        return payload.get("data", {})
    if payload.get("code") == 0:
        return payload.get("data", {})
    return payload


def _find_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("items", "blocks"):
            candidate = value.get(key)
            if isinstance(candidate, list) and all(
                isinstance(item, dict) and "block_id" in item for item in candidate
            ):
                return candidate
        for candidate in value.values():
            found = _find_blocks(candidate)
            if found:
                return found
    elif isinstance(value, list) and all(
        isinstance(item, dict) and "block_id" in item for item in value
    ):
        return value
    return []


def fetch_blocks(token: str, identity: str) -> list[dict[str, Any]]:
    data = _run_lark(
        [
            "api",
            "GET",
                f"/open-apis/docx/v1/documents/{token}/blocks",
                "--as",
                identity,
                "--page-all",
                # Feishu's list-blocks API accepts up to 500 items per page.
                # Larger pages reduce round trips for documents with tables.
                "--page-limit",
                "500",
                "--format",
                "json",
        ]
    )
    blocks = _find_blocks(data)
    if not blocks:
        raise RuntimeError("Feishu returned no document blocks")
    return blocks


def blocks_in_document_order(
    blocks: list[dict[str, Any]], token: str
) -> list[dict[str, Any]]:
    by_id = {block["block_id"]: block for block in blocks}
    roots = [block for block in blocks if block.get("block_type") == 1]
    root = by_id.get(token) or (roots[0] if roots else None)
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(block: dict[str, Any]) -> None:
        block_id = block.get("block_id")
        if not block_id or block_id in visited:
            return
        visited.add(block_id)
        ordered.append(block)
        for child_id in block.get("children") or []:
            child = by_id.get(child_id)
            if child:
                visit(child)

    if root:
        visit(root)
    for block in blocks:
        visit(block)
    return ordered


def _table_size(block: dict[str, Any]) -> tuple[int, int]:
    table = block.get("table") or {}
    properties = table.get("property") or {}
    rows = properties.get("row_size", table.get("row_size", 0))
    columns = properties.get("column_size", table.get("column_size", 0))
    return int(rows or 0), int(columns or 0)


def _text_elements(block: dict[str, Any]) -> list[dict[str, Any]]:
    for key in TEXT_KEYS:
        value = block.get(key)
        if isinstance(value, dict) and isinstance(value.get("elements"), list):
            return value["elements"]
    return []


def _elements_preview(elements: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for element in elements:
        if isinstance(element.get("text_run"), dict):
            parts.append(str(element["text_run"].get("content") or ""))
        elif isinstance(element.get("equation"), dict):
            parts.append(f"${element['equation'].get('content') or ''}$")
    return "".join(parts)


def _normalized_import_text(value: str) -> str:
    while "\\\\" in value:
        value = value.replace("\\\\", "\\")
    return re.sub(r"\s+", " ", value).strip()


def _remove_text_marker(
    elements: list[dict[str, Any]], marker: str
) -> tuple[list[dict[str, Any]], bool]:
    """Remove one exact imported image marker without disturbing other rich text."""
    updated = copy.deepcopy(elements)
    for element in updated:
        text_run = element.get("text_run")
        if not isinstance(text_run, dict):
            continue
        content = str(text_run.get("content") or "")
        if marker not in content:
            continue
        text_run["content"] = content.replace(marker, "", 1)
        return updated, True
    return updated, False


def _validate_public_image_url(value: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("table image URL must use public HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("table image URL must not contain credentials")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or default_port)
    except socket.gaierror as exc:
        raise ValueError("table image host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("table image URL resolved to a non-public address")
    return parsed


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_public_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_table_image(url: str, directory: Path) -> Path:
    parsed = _validate_public_image_url(url)
    suffix = Path(parsed.path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".img"
    target = directory / f"table-image{suffix}"
    opener = urllib.request.build_opener(_PublicRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "somark-to-feishu/0.3"},
    )
    total = 0
    with opener.open(request, timeout=30) as response, target.open("wb") as stream:
        final_url = response.geturl()
        _validate_public_image_url(final_url)
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_TABLE_IMAGE_BYTES:
                raise ValueError("table image exceeds the 20 MB limit")
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"table image URL returned {content_type}")
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_TABLE_IMAGE_BYTES:
                raise ValueError("table image exceeds the 20 MB limit")
            stream.write(chunk)
    if not total:
        raise ValueError("table image download was empty")
    if target.suffix == ".img" and content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            renamed = target.with_suffix(guessed)
            target.rename(renamed)
            target = renamed
    return target


def _media_token(value: Any) -> str:
    candidates = [value]
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        candidates.append(value["data"])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("file_token", "media_token", "token"):
            token = candidate.get(key)
            if isinstance(token, str) and token.strip():
                return token.strip()
    raise RuntimeError("Feishu media upload returned no reusable token")


def _normalized_formula(value: str) -> str:
    while "\\\\" in value:
        value = value.replace("\\\\", "\\")
    return re.sub(r"\s+", " ", value).strip()


def audit_formula_state(
    blocks: list[dict[str, Any]], post_import: dict[str, Any]
) -> dict[str, Any]:
    audit_plan = post_import.get("formula_audit") or {}
    expected = audit_plan.get("expected_formulas") or []
    expected_counter = Counter(
        _normalized_formula(str(item.get("content") or ""))
        for item in expected
        if str(item.get("content") or "").strip()
    )
    actual_counter: Counter[str] = Counter()
    raw_delimiter_blocks: list[str] = []
    by_id = {str(block.get("block_id") or ""): block for block in blocks}
    table_descendants: set[str] = set()

    def mark_table_descendants(block_id: str) -> None:
        if not block_id or block_id in table_descendants:
            return
        table_descendants.add(block_id)
        block = by_id.get(block_id) or {}
        for child_id in block.get("children") or []:
            mark_table_descendants(str(child_id))

    for block in blocks:
        if block.get("block_type") != 31:
            continue
        for cell_id in (block.get("table") or {}).get("cells") or []:
            mark_table_descendants(str(cell_id))
    for block in blocks:
        if str(block.get("block_id") or "") in table_descendants:
            continue
        elements = _text_elements(block)
        preview = _elements_preview(elements)
        if "$$" in preview:
            raw_delimiter_blocks.append(str(block.get("block_id") or ""))
        for element in elements:
            equation = element.get("equation")
            if isinstance(equation, dict):
                content = _normalized_formula(str(equation.get("content") or ""))
                if content:
                    actual_counter[content] += 1
    missing: list[str] = []
    for content, expected_count in expected_counter.items():
        missing.extend([content] * max(0, expected_count - actual_counter[content]))
    return {
        "expected_formula_count": sum(expected_counter.values()),
        "actual_equation_count": sum(actual_counter.values()),
        "missing_formula_count": len(missing),
        "missing_formulas": missing,
        "raw_formula_delimiter_block_ids": raw_delimiter_blocks,
    }


def _build_formula_audit_requests(
    ordered: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    post_import: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    audit_plan = post_import.get("formula_audit") or {}
    operator_plans = audit_plan.get("operator_paragraphs") or []
    state = audit_formula_state(ordered, post_import)
    requests: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped: list[str] = []
    operator_repairs = 0
    text_blocks = [block for block in ordered if _text_elements(block)]

    for plan in operator_plans:
        source = _normalized_import_text(str(plan.get("source_text") or ""))
        prefix = _normalized_import_text(str(plan.get("prefix_text") or ""))
        suffix = _normalized_import_text(str(plan.get("suffix_text") or ""))
        exact = [
            block
            for block in text_blocks
            if source
            and source == _normalized_import_text(
                _elements_preview(_text_elements(block))
            )
        ]
        if len(exact) == 1:
            skipped.append(
                f"Operator formula paragraph {plan.get('paragraph_index', 0)} is valid"
            )
            continue
        candidates = []
        for block in text_blocks:
            preview = _normalized_import_text(
                _elements_preview(_text_elements(block))
            )
            if prefix and prefix not in preview:
                continue
            if suffix and suffix not in preview:
                continue
            if prefix or suffix:
                candidates.append(block)
        if len(candidates) != 1:
            warnings.append(
                "Operator formula paragraph {} could not be located uniquely; "
                "candidates={}".format(
                    plan.get("paragraph_index", 0),
                    [item.get("block_id") for item in candidates[:10]],
                )
            )
            continue
        target = candidates[0]
        requests.append(
            {
                "block_id": target["block_id"],
                "update_text_elements": {"elements": plan.get("elements") or []},
            }
        )
        operator_repairs += 1

    missing_counter = Counter(state["missing_formulas"])
    expected_display = [
        item
        for item in audit_plan.get("expected_formulas") or []
        if item.get("display")
    ]
    used_blocks: set[str] = set()
    for plan in expected_display:
        content = _normalized_formula(str(plan.get("content") or ""))
        if not content or missing_counter[content] <= 0:
            continue
        candidates: list[dict[str, Any]] = []
        for block in text_blocks:
            block_id = str(block.get("block_id") or "")
            if block_id in used_blocks:
                continue
            preview = _normalized_import_text(
                _elements_preview(_text_elements(block))
            )
            if _normalized_import_text(content) in preview and "$$" in preview:
                candidates.append(block)
                continue
            parent = by_id.get(str(block.get("parent_id") or "")) or {}
            siblings = parent.get("children") or []
            try:
                index = siblings.index(block_id)
            except ValueError:
                continue
            adjacent_ids = siblings[max(0, index - 1) : index] + siblings[index + 1 : index + 2]
            if _normalized_import_text(content) in preview and any(
                "$$" in _elements_preview(_text_elements(by_id.get(item) or {}))
                for item in adjacent_ids
            ):
                candidates.append(block)
        if len(candidates) != 1:
            warnings.append(
                "Missing block formula {} could not be located uniquely; candidates={}".format(
                    plan.get("formula_index", 0),
                    [item.get("block_id") for item in candidates[:10]],
                )
            )
            continue
        target = candidates[0]
        parent_id = str(target.get("parent_id") or "")
        parent = by_id.get(parent_id) or {}
        siblings = parent.get("children") or []
        try:
            target_index = siblings.index(target["block_id"])
        except ValueError:
            warnings.append(
                f"Missing block formula {plan.get('formula_index', 0)} has no parent index"
            )
            continue
        start_index = target_index
        end_index = target_index + 1
        if target_index > 0:
            previous = by_id.get(siblings[target_index - 1]) or {}
            if _elements_preview(_text_elements(previous)).strip() == "$$":
                start_index -= 1
                used_blocks.add(str(previous.get("block_id") or ""))
        if target_index + 1 < len(siblings):
            following = by_id.get(siblings[target_index + 1]) or {}
            if _elements_preview(_text_elements(following)).strip() == "$$":
                end_index += 1
                used_blocks.add(str(following.get("block_id") or ""))
        used_blocks.add(str(target.get("block_id") or ""))
        requests.append(
            {
                "block_id": target["block_id"],
                "replace_imported_formula": {
                    "parent_id": parent_id,
                    "start_index": start_index,
                    "end_index": end_index,
                    "content": str(plan.get("content") or "").strip(),
                },
            }
        )
        missing_counter[content] -= 1

    details = {
        **state,
        "formula_audit_warnings": warnings,
        "formula_audit_skipped": skipped,
        "operator_formula_repairs_queued": operator_repairs,
        "block_formula_repairs_queued": sum(
            "replace_imported_formula" in item for item in requests
        ),
    }
    return requests, details


def _image_contexts(
    ordered: list[dict[str, Any]], images: list[dict[str, Any]]
) -> dict[str, tuple[str, str]]:
    positions = {
        block.get("block_id"): index for index, block in enumerate(ordered)
    }
    contexts: dict[str, tuple[str, str]] = {}
    for image in images:
        block_id = str(image.get("block_id") or "")
        position = positions.get(block_id)
        if position is None:
            contexts[block_id] = ("", "")
            continue

        previous_text = ""
        for candidate in reversed(ordered[:position]):
            preview = _normalized_import_text(
                _elements_preview(_text_elements(candidate))
            )
            if preview:
                previous_text = preview[-240:]
                break

        next_text = ""
        for candidate in ordered[position + 1 :]:
            preview = _normalized_import_text(
                _elements_preview(_text_elements(candidate))
            )
            if preview:
                next_text = preview[:240]
                break
        contexts[block_id] = (previous_text, next_text)
    return contexts


def _match_image_plans(
    image_plans: list[dict[str, Any]],
    images: list[dict[str, Any]],
    ordered: list[dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[int],
    list[str],
]:
    if len(image_plans) == len(images):
        return list(zip(image_plans, images)), [], []

    contexts = _image_contexts(ordered, images)
    proposals: dict[int, tuple[int, int]] = {}
    for plan_position, plan in enumerate(image_plans):
        previous_text = _normalized_import_text(
            str(plan.get("previous_text") or "")
        )
        next_text = _normalized_import_text(str(plan.get("next_text") or ""))
        if not previous_text and not next_text:
            continue

        scored: list[tuple[int, int]] = []
        for image_position, image in enumerate(images):
            actual_previous, actual_next = contexts.get(
                str(image.get("block_id") or ""), ("", "")
            )
            score = int(bool(previous_text) and previous_text == actual_previous)
            score += int(bool(next_text) and next_text == actual_next)
            if score:
                scored.append((score, image_position))
        if not scored:
            continue
        best_score = max(score for score, _ in scored)
        best = [item for item in scored if item[0] == best_score]
        if len(best) == 1:
            proposals[plan_position] = best[0]

    image_claims: dict[int, list[int]] = {}
    for plan_position, (_, image_position) in proposals.items():
        image_claims.setdefault(image_position, []).append(plan_position)

    matched_positions = [
        (plan_position, image_position)
        for plan_position, (_, image_position) in proposals.items()
        if len(image_claims[image_position]) == 1
    ]
    matched_positions.sort()

    monotonic: list[tuple[int, int]] = []
    last_image_position = -1
    for plan_position, image_position in matched_positions:
        if image_position <= last_image_position:
            continue
        monotonic.append((plan_position, image_position))
        last_image_position = image_position

    matched_plan_positions = {plan_position for plan_position, _ in monotonic}
    matched_image_positions = {image_position for _, image_position in monotonic}
    matches = [
        (image_plans[plan_position], images[image_position])
        for plan_position, image_position in monotonic
    ]
    unmatched_plans = [
        int(plan.get("image_index", position))
        for position, plan in enumerate(image_plans)
        if position not in matched_plan_positions
    ]
    unmatched_blocks = [
        str(image.get("block_id") or "")
        for position, image in enumerate(images)
        if position not in matched_image_positions
    ]
    return matches, unmatched_plans, unmatched_blocks


def _marker_variants(marker: str) -> list[str]:
    variants = [marker]
    doubled = marker.replace("\\", "\\\\")
    if doubled != marker:
        variants.append(doubled)
    return variants


def _replace_formula_markers(
    elements: list[dict[str, Any]], formulas: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    remaining = [dict(spec) for spec in formulas]
    output: list[dict[str, Any]] = []
    replacements = 0

    for element in elements:
        text_run = element.get("text_run")
        if not isinstance(text_run, dict) or not remaining:
            output.append(element)
            continue
        content = str(text_run.get("content") or "")
        style = dict(text_run.get("text_element_style") or {})
        position = 0
        while position < len(content):
            matches = []
            for index, spec in enumerate(remaining):
                for marker in _marker_variants(spec["marker"]):
                    matches.append(
                        (content.find(marker, position), index, spec, marker)
                    )
            matches = [candidate for candidate in matches if candidate[0] >= 0]
            if not matches:
                if position < len(content):
                    output.append(
                        {
                            "text_run": {
                                "content": content[position:],
                                "text_element_style": style,
                            }
                        }
                    )
                break
            start, index, spec, matched_marker = min(
                matches, key=lambda candidate: candidate[0]
            )
            if start > position:
                output.append(
                    {
                        "text_run": {
                            "content": content[position:start],
                            "text_element_style": style,
                        }
                    }
                )
            output.append(
                {
                    "equation": {
                        "content": spec["content"],
                        "text_element_style": style,
                    }
                }
            )
            position = start + len(matched_marker)
            remaining.pop(index)
            replacements += 1
        if not content:
            output.append(element)

    if remaining:
        existing = [
            str(element.get("equation", {}).get("content") or "").strip()
            for element in elements
            if isinstance(element.get("equation"), dict)
        ]
        for spec in list(remaining):
            if spec["content"].strip() in existing:
                remaining.remove(spec)
    return output, remaining, replacements


def build_patch_requests(
    blocks: list[dict[str, Any]], token: str, post_import: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = blocks_in_document_order(blocks, token)
    by_id = {block["block_id"]: block for block in blocks}
    tables = [block for block in ordered if block.get("block_type") == 31]
    table_descendants: set[str] = set()

    def mark_table_descendants(block_id: str) -> None:
        if not block_id or block_id in table_descendants:
            return
        table_descendants.add(block_id)
        for child_id in (by_id.get(block_id) or {}).get("children") or []:
            mark_table_descendants(str(child_id))

    for table in tables:
        for cell_id in (table.get("table") or {}).get("cells") or []:
            mark_table_descendants(str(cell_id))
    all_images = [block for block in ordered if block.get("block_type") == 27]
    images = [
        block
        for block in all_images
        if str(block.get("block_id") or "") not in table_descendants
    ]
    image_plans = post_import.get("image_captions") or []
    table_plans = post_import.get("table_formulas") or []
    heading_plans = post_import.get("formula_headings") or []
    errors: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []
    requests: list[dict[str, Any]] = []
    formula_replacements = 0

    formula_audit_requests, formula_audit = _build_formula_audit_requests(
        ordered, by_id, post_import
    )
    requests.extend(formula_audit_requests)
    warnings.extend(formula_audit.get("formula_audit_warnings") or [])
    skipped.extend(formula_audit.get("formula_audit_skipped") or [])

    image_pairs, unmatched_image_plans, unmatched_image_blocks = _match_image_plans(
        image_plans, images, ordered
    )
    if len(images) != len(image_plans):
        warnings.append(
            f"Image count mismatch: manifest={len(image_plans)}, Feishu={len(images)}"
        )
        if unmatched_image_plans:
            skipped.append(
                "Image captions skipped because they could not be matched uniquely: "
                + ", ".join(str(index) for index in unmatched_image_plans)
            )
    if len(tables) != len(table_plans):
        warnings.append(
            f"Table count mismatch: manifest={len(table_plans)}, Feishu={len(tables)}"
        )
        table_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if table_plans:
            skipped.append("Table formula enhancement skipped because table counts differ")
    else:
        table_pairs = list(zip(table_plans, tables))

    for plan, block in image_pairs:
        caption = str(plan.get("caption") or "").strip()
        if not caption:
            continue
        image = block.get("image") or {}
        current_caption = str((image.get("caption") or {}).get("content") or "")
        if current_caption == caption:
            skipped.append(f"Image {plan['image_index']} already has the expected caption")
            continue
        image_token = image.get("token")
        if not image_token:
            warnings.append(f"Image {plan['image_index']} has no reusable image token")
            skipped.append(f"Image {plan['image_index']} caption was not applied")
            continue
        requests.append(
            {
                "block_id": block["block_id"],
                "replace_image": {
                    "token": image_token,
                    "caption": {"content": caption},
                },
            }
        )

    for plan, table_block in table_pairs:
        expected = (int(plan["expected_rows"]), int(plan["expected_columns"]))
        actual = _table_size(table_block)
        if actual != expected:
            warnings.append(
                f"Table {plan['table_index']} size mismatch: manifest={expected}, Feishu={actual}"
            )
            continue
        table_cells = (table_block.get("table") or {}).get("cells") or []
        if len(table_cells) != actual[0] * actual[1]:
            warnings.append(
                f"Table {plan['table_index']} returned {len(table_cells)} cell blocks"
            )
            continue
        cell_catalog: list[dict[str, Any]] = []
        for cell_index, cell_id in enumerate(table_cells):
            cell_block = by_id.get(cell_id) or {}
            for child_id in cell_block.get("children") or []:
                text_block = by_id.get(child_id)
                if not text_block:
                    continue
                elements = _text_elements(text_block)
                cell_catalog.append(
                    {
                        "row": cell_index // actual[1],
                        "column": cell_index % actual[1],
                        "text_block": text_block,
                        "elements": elements,
                        "preview": _elements_preview(elements),
                    }
                )
        for image_plan in plan.get("images") or []:
            row = int(image_plan["row"])
            column = int(image_plan["column"])
            if row < 0 or column < 0 or row >= actual[0] or column >= actual[1]:
                warnings.append(
                    f"Table {plan['table_index']} image cell ({row}, {column}) is out of range"
                )
                continue
            cell_index = row * actual[1] + column
            cell_id = str(table_cells[cell_index])
            cell_block = by_id.get(cell_id) or {}
            native_table_images = [
                block
                for block in all_images
                if str(block.get("block_id") or "") in table_descendants
                and (
                    str(block.get("parent_id") or "") == cell_id
                    or str(block.get("block_id") or "")
                    in {
                        str(descendant_id)
                        for descendant_id in cell_block.get("children") or []
                    }
                )
            ]
            if native_table_images:
                native_image = native_table_images[0]
                description = str(image_plan.get("description") or "").strip()
                image = native_image.get("image") or {}
                current_caption = str(
                    (image.get("caption") or {}).get("content") or ""
                ).strip()
                image_token = str(image.get("token") or "").strip()
                if description and image_token and current_caption != description:
                    requests.append(
                        {
                            "block_id": native_image["block_id"],
                            "replace_image": {
                                "token": image_token,
                                "caption": {"content": description},
                            },
                        }
                    )
                else:
                    skipped.append(
                        f"Table {plan['table_index']} image at ({row}, {column}) "
                        "was imported natively"
                    )
                continue
            source_marker = str(image_plan.get("source_marker") or "").strip()
            cleanup: dict[str, Any] | None = None
            if source_marker:
                marker_candidates = [
                    item
                    for item in cell_catalog
                    if item["row"] == row
                    and item["column"] == column
                    and source_marker in item["preview"]
                ]
                if len(marker_candidates) == 1:
                    marker_target = marker_candidates[0]
                    updated_elements, removed = _remove_text_marker(
                        marker_target["elements"], source_marker
                    )
                    if removed:
                        cleanup = {
                            "block_id": marker_target["text_block"]["block_id"],
                            "elements": updated_elements,
                        }
                if cleanup is None:
                    warnings.append(
                        f"Table {plan['table_index']} image marker at ({row}, {column}) "
                        "could not be located exactly; native image will still be inserted"
                    )
            requests.append(
                {
                    "block_id": cell_id,
                    "insert_table_image": {
                        "table_index": int(plan["table_index"]),
                        "row": row,
                        "column": column,
                        "source_url": str(image_plan.get("source_url") or ""),
                        "caption": str(image_plan.get("description") or "").strip(),
                        "index": len(cell_block.get("children") or []),
                        **({"cleanup": cleanup} if cleanup is not None else {}),
                    },
                }
            )
        used_text_blocks: set[str] = set()
        for cell_plan in plan.get("cells") or []:
            row = int(cell_plan["row"])
            column = int(cell_plan["column"])
            source_text = _normalized_import_text(
                str(cell_plan.get("source_text") or "")
            )
            available = [
                item
                for item in cell_catalog
                if item["text_block"]["block_id"] not in used_text_blocks
            ]
            source_candidates = [
                item
                for item in available
                if source_text
                and source_text == _normalized_import_text(item["preview"])
            ]
            marker_candidates = [
                item
                for item in available
                if all(
                    _normalized_import_text(spec["marker"])
                    in _normalized_import_text(item["preview"])
                    for spec in cell_plan.get("formulas") or []
                )
            ]
            candidates = source_candidates or marker_candidates
            preferred = [
                item
                for item in candidates
                if item["row"] == row and item["column"] == column
            ]
            if len(candidates) == 1:
                target = candidates[0]
            elif len(preferred) == 1:
                target = preferred[0]
            else:
                previews = [
                    f"({item['row']}, {item['column']}): {item['preview']}"
                    for item in candidates[:5]
                ]
                if not previews:
                    previews = [
                        f"({item['row']}, {item['column']}): {item['preview']}"
                        for item in cell_catalog
                        if item["preview"]
                    ][:30]
                warnings.append(
                    f"Table {plan['table_index']} source cell ({row}, {column}) "
                    f"could not be located uniquely; candidates={previews}"
                )
                continue
            text_block = target["text_block"]
            elements = _text_elements(text_block)
            updated, unmatched, replacements = _replace_formula_markers(
                elements, cell_plan.get("formulas") or []
            )
            if unmatched:
                markers = ", ".join(spec["marker"] for spec in unmatched)
                actual_elements = json.dumps(elements, ensure_ascii=False)
                if len(actual_elements) > 500:
                    actual_elements = actual_elements[:497] + "..."
                warnings.append(
                    f"Table {plan['table_index']} cell ({row}, {column}) did not contain: "
                    f"{markers}; actual={actual_elements}"
                )
                continue
            used_text_blocks.add(text_block["block_id"])
            if replacements:
                requests.append(
                    {
                        "block_id": text_block["block_id"],
                        "update_text_elements": {"elements": updated},
                    }
                )
                formula_replacements += replacements
            else:
                skipped.append(
                    f"Table {plan['table_index']} source cell ({row}, {column}) already uses native equations"
                )

    used_heading_blocks: set[str] = set()
    text_blocks = [
        block
        for block in ordered
        if 2 <= int(block.get("block_type") or 0) <= 11
        and block.get("parent_id")
    ]
    for plan in heading_plans:
        heading_index = int(plan.get("heading_index") or 0)
        level = int(plan.get("level") or 0)
        if not 1 <= level <= 9:
            warnings.append(
                f"Formula heading {heading_index} has invalid level: {level}"
            )
            continue
        expected_type = level + 2
        source_text = _normalized_import_text(str(plan.get("source_text") or ""))
        candidates = [
            block
            for block in text_blocks
            if block["block_id"] not in used_heading_blocks
            and source_text
            and source_text
            == _normalized_import_text(_elements_preview(_text_elements(block)))
        ]
        existing = [
            block for block in candidates if int(block.get("block_type") or 0) == expected_type
        ]
        paragraphs = [
            block for block in candidates if int(block.get("block_type") or 0) == 2
        ]
        if len(existing) == 1 and not paragraphs:
            used_heading_blocks.add(existing[0]["block_id"])
            skipped.append(
                f"Formula heading {heading_index} already uses heading{level}"
            )
            continue
        if len(paragraphs) != 1 or existing:
            previews = [
                f"type={block.get('block_type')} id={block.get('block_id')}"
                for block in candidates[:10]
            ]
            warnings.append(
                f"Formula heading {heading_index} could not be located uniquely; "
                f"source={source_text!r}; candidates={previews}"
            )
            continue
        target = paragraphs[0]
        parent_id = str(target.get("parent_id") or "")
        parent = by_id.get(parent_id) or {}
        siblings = parent.get("children") or []
        try:
            sibling_index = siblings.index(target["block_id"])
        except ValueError:
            warnings.append(
                f"Formula heading {heading_index} is missing from parent children"
            )
            continue
        elements = _text_elements(target)
        if not elements:
            warnings.append(f"Formula heading {heading_index} has no rich-text elements")
            continue
        used_heading_blocks.add(target["block_id"])
        requests.append(
            {
                "block_id": target["block_id"],
                "replace_text_block_with_heading": {
                    "parent_id": parent_id,
                    "index": sibling_index,
                    "level": level,
                    "elements": elements,
                },
            }
        )

    batch_update_requests = [
        request for request in requests if "insert_table_image" not in request
    ]
    duplicate_ids = {
        request["block_id"]
        for request in batch_update_requests
        if sum(
            item["block_id"] == request["block_id"]
            for item in batch_update_requests
        )
        > 1
    }
    if duplicate_ids:
        errors.append(f"Duplicate Block updates: {', '.join(sorted(duplicate_ids))}")
        requests = []
    report = {
        "errors": errors,
        "warnings": warnings,
        "skipped": skipped,
        "actual_images": len(all_images),
        "actual_body_images": len(images),
        "actual_table_images": len(all_images) - len(images),
        "actual_tables": len(tables),
        "image_plans_matched": len(image_pairs),
        "unmatched_image_plan_indexes": unmatched_image_plans,
        "unmatched_feishu_image_block_ids": unmatched_image_blocks,
        "image_captions_queued": sum("replace_image" in item for item in requests),
        "table_images_queued": sum(
            "insert_table_image" in item for item in requests
        ),
        "table_image_captions_queued": sum(
            "replace_image" in item
            and str(item.get("block_id") or "") in table_descendants
            for item in requests
        ),
        "table_cells_queued": max(
            0,
            sum("update_text_elements" in item for item in requests)
            - int(formula_audit.get("operator_formula_repairs_queued") or 0),
        ),
        "formula_replacements_queued": formula_replacements,
        "formula_headings_queued": sum(
            "replace_text_block_with_heading" in item for item in requests
        ),
        "formula_audit": {
            key: value
            for key, value in formula_audit.items()
            if key not in {"formula_audit_warnings", "formula_audit_skipped"}
        },
    }
    return requests, report


def _apply_table_image(
    token: str, identity: str, request: dict[str, Any]
) -> dict[str, Any]:
    spec = request["insert_table_image"]
    cell_id = str(request["block_id"])
    source_url = str(spec.get("source_url") or "").strip()
    if not source_url:
        raise ValueError("table image plan has no source URL")
    child_index = int(spec.get("index") or 0)
    image_block_id = ""
    with tempfile.TemporaryDirectory(prefix="somark-feishu-table-image-") as directory:
        local_image = _download_table_image(source_url, Path(directory))
        created = _run_lark(
            [
                "api",
                "POST",
                f"/open-apis/docx/v1/documents/{token}/blocks/{cell_id}/children",
                "--as",
                identity,
                "--params",
                json.dumps({"document_revision_id": -1}),
                "--data",
                "-",
                "--format",
                "json",
            ],
            {
                "index": child_index,
                "children": [{"block_type": 27, "image": {}}],
            },
        )
        created_blocks = _find_blocks(created)
        if not created_blocks:
            raise RuntimeError("Feishu created no image block in the target table cell")
        image_block_id = str(created_blocks[0].get("block_id") or "")
        if not image_block_id:
            raise RuntimeError("Feishu returned an image block without block_id")
        try:
            uploaded = _run_lark(
                [
                    "docs",
                    "+media-upload",
                    "--file",
                    str(local_image),
                    "--parent-type",
                    "docx_image",
                    "--parent-node",
                    image_block_id,
                    "--as",
                    identity,
                    "--format",
                    "json",
                ]
            )
            replacement: dict[str, Any] = {"token": _media_token(uploaded)}
            caption = str(spec.get("caption") or "").strip()
            if caption:
                replacement["caption"] = {"content": caption}
            patch_requests: list[dict[str, Any]] = [
                {
                    "block_id": image_block_id,
                    "replace_image": replacement,
                }
            ]
            cleanup = spec.get("cleanup")
            if isinstance(cleanup, dict):
                patch_requests.append(
                    {
                        "block_id": cleanup["block_id"],
                        "update_text_elements": {
                            "elements": cleanup.get("elements") or []
                        },
                    }
                )
            patched = _run_lark(
                [
                    "api",
                    "PATCH",
                    f"/open-apis/docx/v1/documents/{token}/blocks/batch_update",
                    "--as",
                    identity,
                    "--params",
                    json.dumps({"document_revision_id": -1}),
                    "--data",
                    "-",
                    "--format",
                    "json",
                ],
                {"requests": patch_requests},
            )
        except Exception:
            # Do not leave an empty image placeholder in the table when upload
            # or replacement fails. The original Markdown marker is untouched.
            try:
                _run_lark(
                    [
                        "api",
                        "DELETE",
                        f"/open-apis/docx/v1/documents/{token}/blocks/{cell_id}/children/batch_delete",
                        "--as",
                        identity,
                        "--params",
                        json.dumps({"document_revision_id": -1}),
                        "--data",
                        "-",
                        "--format",
                        "json",
                    ],
                    {"start_index": child_index, "end_index": child_index + 1},
                )
            except Exception:
                pass
            raise
    return {
        "table_index": int(spec.get("table_index") or 0),
        "row": int(spec.get("row") or 0),
        "column": int(spec.get("column") or 0),
        "image_block_id": image_block_id,
        "caption_applied": bool(str(spec.get("caption") or "").strip()),
        "response": patched,
    }


def apply_requests(
    token: str, identity: str, requests: list[dict[str, Any]]
) -> list[Any]:
    responses: list[Any] = []
    patch_requests = [
        request
        for request in requests
        if "replace_text_block_with_heading" not in request
        and "replace_imported_formula" not in request
        and "insert_table_image" not in request
    ]
    heading_requests = [
        request for request in requests if "replace_text_block_with_heading" in request
    ]
    formula_rebuild_requests = [
        request for request in requests if "replace_imported_formula" in request
    ]
    table_image_requests = [
        request for request in requests if "insert_table_image" in request
    ]
    for offset in range(0, len(patch_requests), DOCX_BATCH_UPDATE_SIZE):
        chunk = patch_requests[offset : offset + DOCX_BATCH_UPDATE_SIZE]
        responses.append(
            _run_lark(
                [
                    "api",
                    "PATCH",
                    f"/open-apis/docx/v1/documents/{token}/blocks/batch_update",
                    "--as",
                    identity,
                    "--params",
                    json.dumps({"document_revision_id": -1}),
                    "--data",
                    "-",
                    "--format",
                    "json",
                ],
                {"requests": chunk},
            )
        )
    for request in table_image_requests:
        spec = request["insert_table_image"]
        try:
            responses.append(
                {"table_image_applied": _apply_table_image(token, identity, request)}
            )
        except Exception as exc:
            responses.append(
                {
                    "table_image_error": str(exc),
                    "table_index": int(spec.get("table_index") or 0),
                    "row": int(spec.get("row") or 0),
                    "column": int(spec.get("column") or 0),
                }
            )
    for request in heading_requests:
        replacement = request["replace_text_block_with_heading"]
        level = int(replacement["level"])
        parent_id = replacement["parent_id"]
        index = int(replacement["index"])
        created = _run_lark(
            [
                "api",
                "POST",
                f"/open-apis/docx/v1/documents/{token}/blocks/{parent_id}/children",
                "--as",
                identity,
                "--params",
                json.dumps({"document_revision_id": -1}),
                "--data",
                "-",
                "--format",
                "json",
            ],
            {
                "index": index + 1,
                "children": [
                    {
                        "block_type": level + 2,
                        f"heading{level}": {"elements": replacement["elements"]},
                    }
                ],
            },
        )
        deleted = _run_lark(
            [
                "api",
                "DELETE",
                f"/open-apis/docx/v1/documents/{token}/blocks/{parent_id}/children/batch_delete",
                "--as",
                identity,
                "--params",
                json.dumps({"document_revision_id": -1}),
                "--data",
                "-",
                "--format",
                "json",
            ],
            {"start_index": index, "end_index": index + 1},
        )
        responses.append({"heading_created": created, "paragraph_deleted": deleted})
    formula_rebuild_requests.sort(
        key=lambda item: (
            str(item["replace_imported_formula"]["parent_id"]),
            int(item["replace_imported_formula"]["start_index"]),
        ),
        reverse=True,
    )
    for request in formula_rebuild_requests:
        replacement = request["replace_imported_formula"]
        parent_id = replacement["parent_id"]
        start_index = int(replacement["start_index"])
        end_index = int(replacement["end_index"])
        created = _run_lark(
            [
                "api",
                "POST",
                f"/open-apis/docx/v1/documents/{token}/blocks/{parent_id}/children",
                "--as",
                identity,
                "--params",
                json.dumps({"document_revision_id": -1}),
                "--data",
                "-",
                "--format",
                "json",
            ],
            {
                "index": end_index,
                "children": [
                    {
                        "block_type": 2,
                        "text": {
                            "elements": [
                                {
                                    "equation": {
                                        "content": replacement["content"],
                                        "text_element_style": {},
                                    }
                                }
                            ]
                        },
                    }
                ],
            },
        )
        deleted = _run_lark(
            [
                "api",
                "DELETE",
                f"/open-apis/docx/v1/documents/{token}/blocks/{parent_id}/children/batch_delete",
                "--as",
                identity,
                "--params",
                json.dumps({"document_revision_id": -1}),
                "--data",
                "-",
                "--format",
                "json",
            ],
            {"start_index": start_index, "end_index": end_index},
        )
        responses.append({"formula_created": created, "broken_blocks_deleted": deleted})
    return responses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the Block API plan from a SoMark-to-Feishu manifest."
    )
    parser.add_argument("--document", required=True, help="Feishu docx URL or token")
    parser.add_argument("--manifest", required=True, help="V0.2 manifest JSON")
    parser.add_argument("--report", help="Output report JSON path")
    parser.add_argument("--as", dest="identity", choices=("user", "bot"), default="user")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    token = document_id(args.document)
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.is_file():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else manifest_path.with_name(f"{manifest_path.stem}.block-report.json")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest_version = manifest.get("version")
    if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_MANIFEST_VERSIONS))
        print(
            f"The manifest version must be one of: {supported}",
            file=sys.stderr,
        )
        return 1
    blocks = fetch_blocks(token, args.identity)
    requests, details = build_patch_requests(blocks, token, manifest.get("post_import") or {})
    report: dict[str, Any] = {
        "version": "0.2.2",
        "manifest_version": manifest_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "document_id": token,
        "document_url": args.document,
        "dry_run": args.dry_run,
        **details,
        "request_count": len(requests),
        "status": "validation_failed" if details.get("errors") else (
            "ready_with_warnings" if details.get("warnings") else "ready"
        ),
    }
    if not details.get("errors") and not args.dry_run and requests:
        responses = apply_requests(token, args.identity, requests)
        runtime_errors = [
            response
            for response in responses
            if isinstance(response, dict) and response.get("table_image_error")
        ]
        if runtime_errors:
            report["runtime_errors"] = runtime_errors
        verified_blocks = fetch_blocks(token, args.identity)
        report["post_apply_formula_audit"] = audit_formula_state(
            verified_blocks, manifest.get("post_import") or {}
        )
        report["status"] = (
            "partially_applied"
            if details.get("warnings")
            or runtime_errors
            or report["post_apply_formula_audit"].get("missing_formula_count")
            or report["post_apply_formula_audit"].get(
                "raw_formula_delimiter_block_ids"
            )
            else "applied"
        )
        report["batch_count"] = len(responses)
    elif not details.get("errors") and not requests:
        report["status"] = (
            "enhancement_skipped" if details.get("warnings") else "nothing_to_apply"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if details.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
