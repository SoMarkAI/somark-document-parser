"""Create-only DingTalk sheet execution with bounded verification and recovery."""

from __future__ import annotations

from hashlib import sha256
import mimetypes
from pathlib import Path
import re
from time import monotonic, sleep
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .artifacts import RouteName, RouteResult, RouteTarget, SourceArtifacts
from .dws_runner import DwsRunResult, DwsRunner
from .errors import ErrorKind, StructuredError, redact_sensitive
from .manifest import ManifestStage, read_manifest, set_stage, write_manifest_atomic
from .sheet_models import SheetPlan, StylePlan, WorksheetPlan
from .sheet_planner import plan_sheet_route
from .sheet_reconstruct import is_remote_image_reference


_CELL_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_RANGE_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)$")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
_CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
_MAX_IMAGE_BYTES = 50 * 1024 * 1024


class _RouteFailure(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        super().__init__(error.message)
        self.error = error


def download_remote_image(
    reference: str,
    destination_dir: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = _MAX_IMAGE_BYTES,
) -> Path:
    """Materialize one SoMark table-image URL after the preview is ready."""

    if not is_remote_image_reference(reference):
        raise ValueError("image reference is not an HTTP(S) URL")
    parsed = urlparse(reference.strip())
    if parsed.username or parsed.password:
        raise ValueError("image URL must not contain credentials")
    request = Request(reference, headers={"User-Agent": "SoMark-DingTalk-Adapter/1"})
    destination_dir.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type", ""))
        content_type = content_type.split(";", 1)[0].strip().casefold()
        suffix = Path(parsed.path).suffix.casefold()
        if suffix not in _IMAGE_EXTENSIONS:
            suffix = _CONTENT_TYPE_EXTENSIONS.get(content_type, "")
        if not suffix or (
            content_type and not content_type.startswith("image/")
            and Path(parsed.path).suffix.casefold() not in _IMAGE_EXTENSIONS
        ):
            raise ValueError(f"remote resource is not a supported image: {content_type or 'unknown'}")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("remote image exceeds the download size limit")
        body = bytearray()
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("remote image exceeds the download size limit")
    digest = sha256(reference.encode("utf-8")).hexdigest()[:20]
    destination = destination_dir / f"remote_{digest}{suffix}"
    destination.write_bytes(body)
    return destination


def _container(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _string_at(payload: Any, *keys: str) -> str | None:
    """Read only the documented top-level/data fields, never a recursive guessed ID."""

    root = payload if isinstance(payload, Mapping) else {}
    data = root.get("data") if isinstance(root.get("data"), Mapping) else {}
    for mapping in (root, data):
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _required_string(payload: Any, label: str, *keys: str) -> str:
    value = _string_at(payload, *keys)
    if value is None:
        raise _RouteFailure(
            StructuredError(ErrorKind.BUSINESS_VALIDATION, f"DWS response omitted required {label}")
        )
    return value


def _listed_sheets(payload: Any) -> list[Mapping[str, Any]]:
    root = payload if isinstance(payload, Mapping) else {}
    data = root.get("data") if isinstance(root.get("data"), Mapping) else {}
    for mapping in (root, data):
        for key in ("sheets", "items"):
            value = mapping.get(key)
            if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
                return value
    return []


def _sheet_id(payload: Any) -> str:
    sheets = _listed_sheets(payload)
    if not sheets:
        raise _RouteFailure(
            StructuredError(ErrorKind.BUSINESS_VALIDATION, "sheet list returned no worksheets")
        )
    value = sheets[0].get("sheetId")
    if not isinstance(value, str) or not value.strip():
        raise _RouteFailure(
            StructuredError(ErrorKind.BUSINESS_VALIDATION, "sheet list omitted the first exact sheetId")
        )
    return value.strip()


def _cell_value(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("value")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _read_raw_matrix(payload: Any) -> list[list[Any]]:
    container = _container(payload)
    raw = container.get("cells")
    if not isinstance(raw, list):
        raw = container.get("values")
    if not isinstance(raw, list):
        return []
    result: list[list[Any]] = []
    for row in raw:
        if not isinstance(row, list):
            return []
        result.append(
            [item.get("value") if isinstance(item, Mapping) else item for item in row]
        )
    return result


def _read_matrix(payload: Any) -> list[list[str]]:
    return [[_cell_value(item) for item in row] for row in _read_raw_matrix(payload)]


def _same_row(expected: list[str], actual: list[str]) -> bool:
    width = len(expected)
    normalized = actual[:width] + [""] * max(0, width - len(actual))
    return expected == normalized


def _observed_sheet_id(payload: Any) -> str | None:
    return _string_at(payload, "sheetId")


def _observed_range(payload: Any) -> str | None:
    return _string_at(payload, "range", "updatedRange", "a1Notation")


def _check_observed_sheet(payload: Any, expected: str) -> None:
    observed = _observed_sheet_id(payload)
    if observed is not None and observed != expected:
        raise _RouteFailure(
            StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "DWS response sheetId did not match the newly created worksheet",
                details={"expected_sheet_id": expected, "observed_sheet_id": observed},
            )
        )


def _check_observed_range(payload: Any, expected: str) -> None:
    observed = _observed_range(payload)
    if observed is not None and observed != expected:
        raise _RouteFailure(
            StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "DWS response range did not match the requested A1 range",
                details={"expected_range": expected, "observed_range": observed},
            )
        )


def _check_info_sheet(payload: Any, expected: str) -> None:
    _check_observed_sheet(payload, expected)
    container = _container(payload)
    observed = container.get("id")
    if observed is not None and observed != expected:
        raise _RouteFailure(
            StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "sheet info id did not match the newly created worksheet",
                details={"expected_sheet_id": expected, "observed_sheet_id": observed},
            )
        )


def _error_result(error: StructuredError) -> dict[str, Any]:
    return error.to_safe_dict()


def _record_call(result: RouteResult, label: str, run: DwsRunResult, attempt: int) -> None:
    result.ledger.append(
        {
            "kind": "dws_call",
            "label": label,
            "attempt": attempt,
            **run.to_safe_dict(),
        }
    )


def _run_call(
    runner: DwsRunner,
    target: RouteTarget,
    result: RouteResult,
    label: str,
    arguments: Sequence[str],
    *,
    dry_run: bool = False,
) -> Any:
    """Run once, retry only an explicitly retryable service failure, and keep evidence."""

    for attempt in (1, 2):
        run = runner.run_json(arguments, profile=target.profile, dry_run=dry_run)
        _record_call(result, label, run, attempt)
        if run.command_succeeded:
            return run.stdout
        error = run.error or StructuredError(ErrorKind.PROCESS_FAILURE, "DWS command failed")
        if attempt == 1 and error.may_retry_once:
            sleep(min(30.0, max(0.0, error.retry_after_seconds or 0.0)))
            continue
        raise _RouteFailure(error)
    raise AssertionError("unreachable retry state")


def _chunk_entries(plan: SheetPlan) -> list[dict[str, Any]]:
    return [
        {
            "kind": "value_chunk",
            "worksheet_index": worksheet.index,
            "chunk_index": chunk.index,
            "range": chunk.range,
            "content_sha256": chunk.content_sha256,
            "status": chunk.status,
        }
        for worksheet in plan.worksheets
        for chunk in worksheet.value_chunks
    ]


def _persist(plan: SheetPlan, result: RouteResult) -> None:
    if not plan.manifest_path:
        return
    manifest = read_manifest(plan.manifest_path)
    manifest["target"] = {**manifest["target"], **redact_sensitive(result.target), "direct_url": result.direct_url}
    manifest["timings"] = dict(result.timings)
    manifest["statistics"] = dict(result.statistics)
    manifest["degradations"] = list(result.degradations)
    manifest["warnings"] = list(result.warnings)
    manifest["ledger"] = _chunk_entries(plan) + list(result.ledger)
    manifest["readback"] = redact_sensitive(dict(result.readback))
    stage = ManifestStage(result.stage)
    set_stage(manifest, stage, error=result.error)
    write_manifest_atomic(plan.manifest_path, manifest)


def _fail(plan: SheetPlan, result: RouteResult, error: StructuredError) -> RouteResult:
    result.stage = ManifestStage.FAILED.value
    result.error = _error_result(error)
    _persist(plan, result)
    return result


def _anchor_rows(worksheet: WorksheetPlan, start: int, end: int) -> list[int]:
    candidates = [start]
    candidates.extend(row for row in worksheet.duplicate_header_rows if start <= row <= end)
    candidates.extend(row for row in worksheet.fully_empty_rows if start <= row <= end)
    candidates.append(end)
    return list(dict.fromkeys(candidates))


def _read_args(node_id: str, sheet_id: str, range_name: str, mode: str = "raw_value") -> list[str]:
    return [
        "sheet", "range", "read", "--node", node_id, "--sheet-id", sheet_id,
        "--range", range_name, "--value-render-option", mode,
    ]


def _diagnose_anchors(
    runner: DwsRunner,
    target: RouteTarget,
    result: RouteResult,
    worksheet: WorksheetPlan,
    node_id: str,
    sheet_id: str,
    rows: list[int],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    last_column = _column_name(worksheet.column_count - 1)
    for row in rows:
        range_name = f"A{row}:{last_column}{row}"
        try:
            payload = _run_call(
                runner, target, result, f"diagnostic_read_{range_name}",
                _read_args(node_id, sheet_id, range_name),
            )
            _check_observed_sheet(payload, sheet_id)
            observed = (_read_matrix(payload) or [[]])[0]
            diagnostics.append(
                {"row": row, "range": range_name, "matches": _same_row(worksheet.rows[row - 1], observed)}
            )
        except _RouteFailure as exc:
            diagnostics.append({"row": row, "range": range_name, "error": exc.error.to_safe_dict()})
    return diagnostics


def _verify_chunk(
    runner: DwsRunner,
    target: RouteTarget,
    result: RouteResult,
    worksheet: WorksheetPlan,
    node_id: str,
    sheet_id: str,
    start_row: int,
    end_row: int,
    range_name: str,
) -> dict[str, Any]:
    payload = _run_call(
        runner, target, result, f"base_read_{range_name}",
        _read_args(node_id, sheet_id, range_name),
    )
    _check_observed_sheet(payload, sheet_id)
    _check_observed_range(payload, range_name)
    raw_matrix = _read_raw_matrix(payload)
    matrix = [[_cell_value(item) for item in row] for row in raw_matrix]
    expected_rows = end_row - start_row + 1
    expected_columns = worksheet.column_count
    shape_matches = len(matrix) == expected_rows and all(
        len(row) == expected_columns for row in matrix
    )
    text_types_match = all(
        value is None or isinstance(value, str)
        for row in raw_matrix
        for value in row
    )
    container = _container(payload)
    row_indices = container.get("rowIndices")
    column_indices = container.get("colIndices")
    indices_match = (
        (not isinstance(row_indices, list) or row_indices == list(range(start_row, end_row + 1)))
        and (
            not isinstance(column_indices, list)
            or column_indices == [_column_name(index) for index in range(expected_columns)]
        )
    )
    anchors = _anchor_rows(worksheet, start_row, end_row)
    checks: list[dict[str, Any]] = []
    for row in anchors:
        local_index = row - start_row
        actual = matrix[local_index] if 0 <= local_index < len(matrix) else []
        checks.append({"row": row, "matches": _same_row(worksheet.rows[row - 1], actual)})
    if (
        not shape_matches
        or not text_types_match
        or not indices_match
        or not all(item["matches"] for item in checks)
    ):
        diagnostics = _diagnose_anchors(
            runner, target, result, worksheet, node_id, sheet_id, anchors
        )
        raise _RouteFailure(
            StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                f"minimum readback mismatch for {range_name}",
                details={
                    "expected_shape": [expected_rows, expected_columns],
                    "observed_shape": [len(matrix), max((len(row) for row in matrix), default=0)],
                    "plain_text_types_match": text_types_match,
                    "indices_match": indices_match,
                    "anchor_checks": checks,
                    "diagnostics": diagnostics,
                },
            )
        )
    return {
        "range": range_name,
        "sheet_id": sheet_id,
        "row_count": expected_rows,
        "column_count": expected_columns,
        "cell_count": expected_rows * expected_columns,
        "plain_text_types_match": text_types_match,
        "anchor_checks": checks,
    }


def _column_name(index: int) -> str:
    value = index + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _parse_cell(cell: str) -> tuple[int, int]:
    match = _CELL_RE.fullmatch(cell)
    if not match:
        raise ValueError(f"invalid planned cell: {cell}")
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - 64
    return int(match.group(2)), column


def _expected_range(worksheet: WorksheetPlan, range_name: str) -> list[list[str]]:
    match = _RANGE_RE.fullmatch(range_name)
    if not match:
        match_cell = _CELL_RE.fullmatch(range_name)
        if not match_cell:
            raise ValueError(f"unsupported planned range: {range_name}")
        start_row, start_col = _parse_cell(range_name)
        end_row, end_col = start_row, start_col
    else:
        start_row, start_col = _parse_cell(match.group(1) + match.group(2))
        end_row, end_col = _parse_cell(match.group(3) + match.group(4))
    return [row[start_col - 1 : end_col] for row in worksheet.rows[start_row - 1 : end_row]]


def controlled_overwrite_allowed(expected: list[list[str]], observed_payload: Any) -> bool:
    """Allow enhancement overwrite only when a fresh read still equals the planned base value."""

    observed = _read_matrix(observed_payload)
    if len(expected) != len(observed):
        return False
    return all(_same_row(expected_row, observed_row) for expected_row, observed_row in zip(expected, observed))


def _style_groups(styles: list[StylePlan]) -> list[StylePlan]:
    """Coalesce adjacent same-row cell styles without changing their declared semantics."""

    groups: list[StylePlan] = []
    for style in styles:
        if not groups or groups[-1].options != style.options:
            groups.append(style)
            continue
        previous = groups[-1]
        prev_match = _CELL_RE.fullmatch(previous.range.split(":")[-1])
        current_match = _CELL_RE.fullmatch(style.range)
        if not prev_match or not current_match:
            groups.append(style)
            continue
        prev_row, prev_col = _parse_cell(prev_match.group(0))
        row, column = _parse_cell(current_match.group(0))
        if row == prev_row and column == prev_col + 1:
            start = previous.range.split(":", 1)[0]
            groups[-1] = StylePlan(f"{start}:{style.range}", previous.options)
        else:
            groups.append(style)
    return groups


def _style_args(node_id: str, sheet_id: str, style: StylePlan) -> list[str]:
    arguments = [
        "sheet", "range", "set-style", "--node", node_id, "--sheet-id", sheet_id,
        "--range", style.range,
    ]
    for key, value in style.options.items():
        arguments.extend((f"--{key}", str(value)))
    return arguments


def enhance_sheet_route(
    plan: SheetPlan,
    target: RouteTarget,
    *,
    runner: DwsRunner,
    node_id: str,
    sheet_ids: Sequence[str],
    base_result: RouteResult | None = None,
    fast_mode: bool = True,
) -> RouteResult:
    """Enhance the already-created object; this function never creates another workbook."""

    result = base_result or RouteResult(route=RouteName.SHEET)
    result.degradations = list(dict.fromkeys([*result.degradations, *plan.degradations]))
    if not plan.worksheets or len(sheet_ids) != len(plan.worksheets):
        return _fail(
            plan,
            result,
            StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "enhancement requires one exact created worksheet ID per planned worksheet",
                details={
                    "planned_worksheet_count": len(plan.worksheets),
                    "created_worksheet_id_count": len(sheet_ids),
                },
            ),
        )

    enhancement_failures: list[str] = []
    worksheet_readbacks: list[dict[str, Any]] = []
    total_style_ranges = 0
    total_verified_merges = 0
    total_dimensions = 0
    total_uploaded_images = 0
    image_cache_dir = Path(plan.evidence_dir) / "downloaded_table_images"
    downloaded_images: dict[str, Path] = {}

    def attempt(label: str, arguments: Sequence[str], *, dry_run: bool = False) -> Any | None:
        try:
            return _run_call(runner, target, result, label, arguments, dry_run=dry_run)
        except _RouteFailure as exc:
            enhancement_failures.append(f"{label}: {exc.error.message}")
            return None

    for worksheet, sheet_id in zip(plan.worksheets, sheet_ids):
        label_prefix = f"worksheet_{worksheet.index}_"
        enhancement_readback: dict[str, Any] = {
            "worksheet_index": worksheet.index,
            "worksheet_name": worksheet.name,
            "sheet_id": sheet_id,
        }
        style_groups = _style_groups(worksheet.styles)
        total_style_ranges += len(style_groups)
        style_entries: list[dict[str, Any]] = []
        for batch_index, style in enumerate(style_groups, start=1):
            entry = {
                "kind": "style_batch",
                "worksheet_index": worksheet.index,
                "batch_index": batch_index,
                "range": style.range,
                "status": "pending",
            }
            result.ledger.append(entry)
            style_entries.append(entry)
            entry["status"] = "running"
            payload = attempt(
                f"{label_prefix}style_{style.range}",
                _style_args(node_id, sheet_id, style),
            )
            if payload is not None:
                _check_observed_sheet(payload, sheet_id)
                entry["status"] = "written"
            else:
                entry["status"] = "failed"

        # Validate every merge against the verified base values before images change
        # their anchor cells.  DingTalk rejects image writes into cells that have
        # already been merged, so the actual merge calls must happen after images.
        merge_candidates: list[Any] = []
        for merge in worksheet.merges:
            read_payload = attempt(
                f"{label_prefix}premerge_read_{merge.range}",
                _read_args(node_id, sheet_id, merge.range, "raw_value"),
            )
            if read_payload is None:
                continue
            if not controlled_overwrite_allowed(_expected_range(worksheet, merge.range), read_payload):
                enhancement_failures.append(
                    f"{label_prefix}merge_{merge.range}: target cells changed after base verification; merge was refused"
                )
                continue
            merge_candidates.append(merge)

        for dimension in worksheet.dimensions:
            payload = attempt(
                f"{label_prefix}dimension_{dimension.dimension}_{dimension.start_index}",
                [
                    "sheet", "update-dimension", "--node", node_id, "--sheet-id", sheet_id,
                    "--dimension", dimension.dimension, "--start-index", dimension.start_index,
                    "--length", str(dimension.length), "--pixel-size", str(dimension.pixel_size),
                ],
            )
            if payload is not None:
                _check_observed_sheet(payload, sheet_id)
                total_dimensions += 1

        uploaded_images = []
        for image in worksheet.images:
            image_path = Path(image.local_path) if image.local_path else None
            if image_path is None or not image_path.is_file():
                reference = image.source_reference.strip()
                if reference in downloaded_images:
                    image_path = downloaded_images[reference]
                elif is_remote_image_reference(reference):
                    try:
                        image_path = download_remote_image(reference, image_cache_dir)
                    except (OSError, ValueError) as exc:
                        enhancement_failures.append(
                            f"{label_prefix}image_{image.cell}: remote image download failed: {exc}"
                        )
                        continue
                    downloaded_images[reference] = image_path
                else:
                    continue
            arguments = [
                "sheet", "write-image", "--node", node_id, "--sheet-id", sheet_id,
                "--range", f"{image.cell}:{image.cell}", "--file", str(image_path), "--name", image_path.name,
            ]
            mime_type = mimetypes.guess_type(image_path.name)[0]
            if mime_type:
                arguments.extend(("--mime-type", mime_type))
            if image.width:
                arguments.extend(("--width", str(image.width)))
            if image.height:
                arguments.extend(("--height", str(image.height)))
            payload = attempt(f"{label_prefix}image_{image.cell}", arguments)
            if payload is not None:
                _check_observed_sheet(payload, sheet_id)
                uploaded_images.append(image)
        total_uploaded_images += len(uploaded_images)

        verified_merges: list[str] = []
        for merge in merge_candidates:
            payload = attempt(
                f"{label_prefix}merge_{merge.range}",
                [
                    "sheet", "merge-cells", "--node", node_id, "--sheet-id", sheet_id,
                    "--range", merge.range, "--merge-type", "mergeAll",
                ],
            )
            if payload is not None:
                _check_observed_sheet(payload, sheet_id)
                verified_merges.append(merge.range)
        total_verified_merges += len(verified_merges)

        info_include = []
        if any(item.dimension == "ROWS" for item in worksheet.dimensions):
            info_include.append("row_heights")
        if any(item.dimension == "COLUMNS" for item in worksheet.dimensions):
            info_include.append("col_widths")
        info_payload = None
        if worksheet.merges or info_include:
            info_args = ["sheet", "info", "--node", node_id, "--sheet-id", sheet_id]
            if info_include:
                info_args.extend(("--include", ",".join(info_include)))
            info_payload = attempt(f"{label_prefix}verify_sheet_info", info_args)
        if info_payload is not None:
            _check_info_sheet(info_payload, sheet_id)
            info = _container(info_payload)
            merged = info.get("mergedRanges")
            if worksheet.merges and isinstance(merged, list):
                missing_merges = sorted(set(verified_merges).difference(str(item) for item in merged))
                if missing_merges:
                    enhancement_failures.append(
                        f"{label_prefix}sheet info omitted planned merged ranges: " + ", ".join(missing_merges)
                    )
            elif worksheet.merges and merged is None:
                enhancement_failures.append(
                    f"{label_prefix}sheet info returned null/absent mergedRanges for declared merges"
                )
            dimension_response_keys = {
                "row_heights": ("row_heights", "rowHeights"),
                "col_widths": ("col_widths", "colWidths"),
            }
            for key in info_include:
                candidates = dimension_response_keys[key]
                if all(info.get(candidate) is None for candidate in candidates):
                    result.degradations.append(
                        f"[DWS readback] worksheet {worksheet.index} sheet info returned null/absent {key}; "
                        "dimension verification is partial"
                    )
            enhancement_readback["info"] = redact_sensitive(info_payload)

        if worksheet.styles:
            sample_range = style_groups[0].range
            payload = attempt(
                f"{label_prefix}verify_style_sample",
                _read_args(node_id, sheet_id, sample_range, "formatted_value"),
            )
            enhancement_readback["style_sample"] = {
                "range": sample_range,
                "response": redact_sensitive(payload),
            }
            if payload is not None:
                for entry in style_entries:
                    if entry["status"] == "written":
                        entry["status"] = "verified"
            else:
                for entry in style_entries:
                    if entry["status"] == "written":
                        entry["status"] = "failed"
        if uploaded_images:
            sample = uploaded_images[0]
            payload = attempt(
                f"{label_prefix}verify_image_sample",
                _read_args(node_id, sheet_id, sample.cell, "formatted_value"),
            )
            enhancement_readback["image_sample"] = {
                "cell": sample.cell,
                "response": redact_sensitive(payload),
            }
        worksheet_readbacks.append(enhancement_readback)

    result.readback["enhancement"] = {"worksheets": worksheet_readbacks}
    result.degradations.extend(f"[adapter enhancement] {item}" for item in enhancement_failures)
    result.degradations = list(dict.fromkeys(result.degradations))
    result.statistics.update(
        {
            "plain_text_value_written": sum(item.cell_count for item in plan.worksheets),
            "plain_text_policy_enforced": True,
            "style_range_written": total_style_ranges,
            "merge_written": total_verified_merges,
            "dimension_written": total_dimensions,
            "image_written": total_uploaded_images,
            "fast_mode": bool(fast_mode),
        }
    )
    result.stage = ManifestStage.PARTIAL.value if result.degradations else ManifestStage.VERIFIED.value
    _persist(plan, result)
    return result


def run_sheet_route(
    source: SourceArtifacts,
    target: RouteTarget,
    *,
    runner: DwsRunner | None = None,
    execute: bool = False,
    enhance: bool = True,
    fast_mode: bool = True,
    plan_callback: Callable[[Mapping[str, Any]], None] | None = None,
    preview_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> RouteResult:
    """Plan, then optionally create and populate one new DingTalk workbook."""

    started = monotonic()
    plan = plan_sheet_route(source, target)
    result = RouteResult(
        route=RouteName.SHEET,
        stage=ManifestStage.PENDING.value,
        target={
            "title": target.title,
            "profile": "[EXPLICIT]" if target.profile else None,
            "create_only": True,
        },
        statistics=dict(plan.statistics),
        degradations=list(plan.degradations),
        warnings=list(plan.warnings),
        evidence_files=[path for path in (plan.plan_path, plan.manifest_path) if path is not None]
        + [
            path
            for worksheet in plan.worksheets
            for path in (
                worksheet.csv_path,
                worksheet.text_policy_json_path,
                worksheet.style_json_path,
                worksheet.merge_json_path,
                worksheet.dimension_json_path,
                worksheet.image_json_path,
            )
            if path is not None
        ]
        + [chunk.values_path for worksheet in plan.worksheets for chunk in worksheet.value_chunks],
        readback={"required": True, "performed": False},
    )
    result.timings["planning_seconds"] = monotonic() - started
    if plan_callback is not None:
        try:
            plan_callback(
                {
                    "event": "plan_completed",
                    "route": RouteName.SHEET.value,
                    "planning_seconds": result.timings["planning_seconds"],
                    "worksheet_count": len(plan.worksheets),
                    "manifest": plan.manifest_path,
                }
            )
        except Exception as exc:
            result.warnings.append(
                f"Plan callback failed after local planning: {type(exc).__name__}"
            )
    if not execute:
        _persist(plan, result)
        return result
    if not target.profile:
        return _fail(
            plan,
            result,
            StructuredError(
                ErrorKind.PROFILE_REQUIRED,
                "execute=True requires one explicit RouteTarget.profile; no login or profile switching is attempted",
            ),
        )

    active_runner = runner or DwsRunner()
    version, version_error = active_runner.read_version()
    result.statistics["dws_cli_version"] = version
    if version_error:
        return _fail(plan, result, version_error)

    result.stage = ManifestStage.RUNNING.value
    _persist(plan, result)
    execution_started = monotonic()
    try:
        create_payload = _run_call(
            active_runner, target, result, "create_workbook",
            ["sheet", "create", "--name", target.title],
        )
        node_id = _required_string(create_payload, "nodeId", "nodeId")
        direct_url = _string_at(create_payload, "docUrl", "url", "directUrl")
        result.direct_url = direct_url or f"https://alidocs.dingtalk.com/i/nodes/{node_id}"
        result.target.update({"node_id": node_id, "direct_url": result.direct_url})
        list_payload = _run_call(
            active_runner, target, result, "list_worksheets",
            ["sheet", "list", "--node", node_id],
        )
        first_sheet_id = _sheet_id(list_payload)
        first_worksheet = plan.primary
        rename_payload = _run_call(
            active_runner,
            target,
            result,
            "rename_worksheet_1",
            [
                "sheet", "update", "--node", node_id, "--sheet-id", first_sheet_id,
                "--name", first_worksheet.name,
            ],
        )
        _check_observed_sheet(rename_payload, first_sheet_id)
        sheet_ids = [first_sheet_id]
        for worksheet in plan.worksheets[1:]:
            new_payload = _run_call(
                active_runner,
                target,
                result,
                f"create_worksheet_{worksheet.index}",
                ["sheet", "new", "--node", node_id, "--name", worksheet.name],
            )
            sheet_ids.append(_required_string(new_payload, "sheetId", "sheetId"))
        result.target["sheet_ids"] = sheet_ids

        base_reads: list[dict[str, Any]] = []
        verified_chunk_count = 0
        for worksheet, sheet_id in zip(plan.worksheets, sheet_ids):
            for chunk in worksheet.value_chunks:
                chunk.status = "running"
                _persist(plan, result)
                try:
                    values = Path(chunk.values_path).read_text(encoding="utf-8")
                    put_payload = _run_call(
                        active_runner,
                        target,
                        result,
                        f"write_worksheet_{worksheet.index}_chunk_{chunk.index}",
                        [
                            "sheet", "range", "update", "--node", node_id, "--sheet-id", sheet_id,
                            "--range", chunk.range, "--values", values,
                        ],
                    )
                except _RouteFailure as write_failure:
                    # The remote write may have committed even when its response failed. Read first;
                    # never blindly replay a write after an ambiguous transport failure.
                    try:
                        readback = _verify_chunk(
                            active_runner, target, result, worksheet, node_id, sheet_id,
                            chunk.start_row, chunk.end_row, chunk.range,
                        )
                    except _RouteFailure:
                        raise write_failure
                    result.warnings.append(
                        f"Worksheet {worksheet.index} chunk {chunk.index} write response failed, but exact "
                        "minimum readback matched; the chunk was accepted without replay."
                    )
                else:
                    _check_observed_sheet(put_payload, sheet_id)
                    _check_observed_range(put_payload, chunk.range)
                    chunk.status = "written"
                    _persist(plan, result)
                    readback = _verify_chunk(
                        active_runner, target, result, worksheet, node_id, sheet_id,
                        chunk.start_row, chunk.end_row, chunk.range,
                    )
                base_reads.append({"worksheet_index": worksheet.index, **readback})
                chunk.status = "verified"
                verified_chunk_count += 1
                _persist(plan, result)

        result.readback = {
            "required": True,
            "performed": True,
            "base_chunks": base_reads,
            "source_to_target_mapping_count": sum(
                len(worksheet.row_mappings) for worksheet in plan.worksheets
            ),
        }
        result.statistics["executed_worksheet_count"] = len(plan.worksheets)
        result.statistics["verified_value_chunk_count"] = verified_chunk_count
        result.stage = ManifestStage.WRITTEN.value
        _persist(plan, result)
        postprocess_pending = bool(
            any(
                worksheet.styles
                or worksheet.merges
                or worksheet.dimensions
                or worksheet.images
                for worksheet in plan.worksheets
            )
        )
        if preview_callback is not None:
            try:
                preview_callback(
                    {
                        "event": "sheet_preview_ready",
                        "stage": ManifestStage.WRITTEN.value,
                        "nodeId": node_id,
                        "sheetIds": list(sheet_ids),
                        "direct_url": result.direct_url,
                        "postprocess_pending": postprocess_pending,
                    }
                )
            except Exception as exc:
                result.warnings.append(
                    f"Preview callback failed after workbook base write: {type(exc).__name__}"
                )
                _persist(plan, result)
    except _RouteFailure as exc:
        for worksheet in plan.worksheets:
            for chunk in worksheet.value_chunks:
                if chunk.status in {"running", "written"}:
                    chunk.status = "failed"
        result.timings["execution_seconds"] = monotonic() - execution_started
        return _fail(plan, result, exc.error)

    result.timings["execution_seconds"] = monotonic() - execution_started
    if not enhance:
        _persist(plan, result)
        return result
    enhancement_started = monotonic()
    enhanced = enhance_sheet_route(
        plan,
        target,
        runner=active_runner,
        node_id=str(result.target["node_id"]),
        sheet_ids=list(result.target["sheet_ids"]),
        base_result=result,
        fast_mode=fast_mode,
    )
    enhanced.timings["enhancement_seconds"] = monotonic() - enhancement_started
    enhanced.timings["total_seconds"] = monotonic() - started
    _persist(plan, enhanced)
    return enhanced


__all__ = ["controlled_overwrite_allowed", "enhance_sheet_route", "run_sheet_route"]
