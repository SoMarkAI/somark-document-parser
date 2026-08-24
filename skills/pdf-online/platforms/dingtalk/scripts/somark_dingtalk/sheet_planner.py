"""Offline planning and evidence generation for the DingTalk sheet route."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .artifacts import RouteName, RouteTarget, SourceArtifacts
from .manifest import new_manifest, write_manifest_atomic
from .sheet_models import SheetPlan, ValueChunk, WorksheetPlan
from .sheet_reconstruct import a1_range, is_remote_image_reference, load_and_reconstruct


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CELL_SOFT_CAP = 5_000
_ROW_HARD_CAP = 1_000
_VALUES_ARGUMENT_SOFT_CAP = 20_000


def _write_json(path: Path, value: Any) -> str:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _write_csv(path: Path, rows: list[list[str]], columns: int) -> str:
    """Write stable RFC-4180 CSV while keeping every source string unchanged."""

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, dialect="excel", lineterminator="\r\n")
        for row in rows:
            writer.writerow(list(row) + [""] * (columns - len(row)))
    return str(path)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _text_cell(value: str) -> dict[str, Any]:
    # DingTalk may infer numbers, dates, percentages, booleans and formulas
    # even for a type=text object with numberFormat '@'. A leading apostrophe
    # is the spreadsheet literal marker and is omitted from the displayed
    # value, so apply it to every non-empty source string.
    encoded = f"'{value}" if value else value
    return {
        "type": "text",
        "text": encoded,
        "cellStyles": {"numberFormat": "@"},
    }


def _payload_row(row: list[str], columns: int) -> str:
    padded = list(row) + [""] * (columns - len(row))
    return json.dumps(
        [_text_cell(value) for value in padded],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _write_values(path: Path, serialized_rows: list[str]) -> str:
    path.write_text("[" + ",".join(serialized_rows) + "]", encoding="utf-8")
    return str(path)


def _chunk_worksheet(worksheet: WorksheetPlan, directory: Path) -> None:
    columns = worksheet.column_count
    if columns < 1:
        raise ValueError(f"worksheet {worksheet.index} contains no columns")
    chunks: list[ValueChunk] = []
    serialized_rows: list[str] = []
    start_offset = 0

    def flush(end_offset: int) -> None:
        nonlocal serialized_rows, start_offset
        if not serialized_rows:
            return
        chunk_index = len(chunks) + 1
        start_row = start_offset + 1
        end_row = end_offset
        path = directory / f"worksheet_{worksheet.index:02d}_chunk_{chunk_index:03d}.json"
        _write_values(path, serialized_rows)
        chunks.append(
            ValueChunk(
                index=chunk_index,
                start_row=start_row,
                end_row=end_row,
                range=a1_range(start_row, end_row, columns),
                cell_count=len(serialized_rows) * columns,
                values_path=str(path),
                content_sha256=_digest(path),
            )
        )
        serialized_rows = []
        start_offset = end_offset

    for offset, row in enumerate(worksheet.rows):
        serialized = _payload_row(row, columns)
        candidate_size = 2 + sum(len(item) for item in serialized_rows) + len(serialized)
        candidate_size += len(serialized_rows)
        candidate_rows = len(serialized_rows) + 1
        candidate_cells = candidate_rows * columns
        if serialized_rows and (
            candidate_rows > _ROW_HARD_CAP
            or candidate_cells > _CELL_SOFT_CAP
            or candidate_size > _VALUES_ARGUMENT_SOFT_CAP
        ):
            flush(offset)
        serialized_rows.append(serialized)
    flush(worksheet.row_count)
    worksheet.value_chunks = chunks


def _write_worksheet_evidence(worksheet: WorksheetPlan, directory: Path) -> None:
    prefix = f"worksheet_{worksheet.index:02d}"
    worksheet.csv_path = _write_csv(directory / f"{prefix}_full.csv", worksheet.rows, worksheet.column_count)
    worksheet.text_policy_json_path = _write_json(
        directory / f"{prefix}_text_policy.json",
        {
            "storage": "plain_text",
            "number_format": "@",
            "literal_escape": "leading_apostrophe_for_all_nonempty_cells",
            "latex_policy": "simple_to_unicode_complex_preserve_source",
            "native_formula_generation": False,
            "type_inference": False,
            "latex_unicode_cells": worksheet.latex_unicode_cells,
            "latex_source_cells": worksheet.latex_source_cells,
            "literal_formula_cells": worksheet.literal_formula_cells,
        },
    )
    worksheet.style_json_path = _write_json(
        directory / f"{prefix}_styles.json", [vars(item) for item in worksheet.styles]
    )
    worksheet.merge_json_path = _write_json(
        directory / f"{prefix}_merges.json", [vars(item) for item in worksheet.merges]
    )
    worksheet.dimension_json_path = _write_json(
        directory / f"{prefix}_dimensions.json", [vars(item) for item in worksheet.dimensions]
    )
    worksheet.image_json_path = _write_json(
        directory / f"{prefix}_images.json", [vars(item) for item in worksheet.images]
    )
    _chunk_worksheet(worksheet, directory)


def _statistics(worksheets: list[WorksheetPlan]) -> dict[str, Any]:
    return {
        "worksheet_count": len(worksheets),
        "source_table_block_count": sum(len(item.source_blocks) for item in worksheets),
        "source_row_count": sum(item.row_count for item in worksheets),
        "source_column_count": max((item.column_count for item in worksheets), default=0),
        "output_row_count": sum(item.row_count for item in worksheets),
        "output_column_count": max((item.column_count for item in worksheets), default=0),
        "output_cell_count": sum(item.cell_count for item in worksheets),
        "maximum_column_count": max((item.column_count for item in worksheets), default=0),
        "value_chunk_count": sum(len(item.value_chunks) for item in worksheets),
        "duplicate_header_row_count": sum(len(item.duplicate_header_rows) for item in worksheets),
        "fully_empty_row_count": sum(len(item.fully_empty_rows) for item in worksheets),
        "plain_text_cell_count": sum(item.cell_count for item in worksheets),
        "latex_unicode_cell_count": sum(len(item.latex_unicode_cells) for item in worksheets),
        "latex_source_cell_count": sum(len(item.latex_source_cells) for item in worksheets),
        "literal_formula_cell_count": sum(len(item.literal_formula_cells) for item in worksheets),
        "merge_count": sum(len(item.merges) for item in worksheets),
        "skipped_merge_count": sum(len(item.merge_degradations) for item in worksheets),
        "style_count": sum(len(item.styles) for item in worksheets),
        "dimension_count": sum(len(item.dimensions) for item in worksheets),
        "image_count": sum(len(item.images) for item in worksheets),
    }


def plan_sheet_route(source: SourceArtifacts, target: RouteTarget) -> SheetPlan:
    """Build a deterministic sheet plan without invoking DWS or changing DingTalk."""

    if target.route != RouteName.SHEET:
        raise ValueError(f"sheet planner requires target route 'sheet', got {target.route!r}")
    if not _SHA256_RE.fullmatch(source.source_hash):
        raise ValueError("source.source_hash must be a SHA-256 hex digest")
    if not source.json_path:
        raise ValueError("the sheet route requires an explicit SoMark JSON artifact from the current parse")

    evidence = Path(target.evidence_dir).expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    worksheets = load_and_reconstruct(source.json_path, assets_dir=source.assets_dir)
    for worksheet in worksheets:
        _write_worksheet_evidence(worksheet, evidence)

    degradations: list[str] = []
    warnings: list[str] = []
    for worksheet in worksheets:
        for degradation in worksheet.merge_degradations:
            degradations.append(
                f"[merge compatibility] Worksheet {worksheet.index} skipped "
                f"{degradation['range']} ({degradation['reason']}); cell values were kept."
            )
        missing = sum(
            1
            for item in worksheet.images
            if not item.local_path and not is_remote_image_reference(item.source_reference)
        )
        if missing:
            degradations.append(
                f"[SoMark asset] Worksheet {worksheet.index} has {missing} image reference(s) without an exact local asset; "
                "their source text/reference is preserved but image upload is unavailable. "
                "HTTP(S) references are downloaded later during spreadsheet enhancement."
            )
        if worksheet.latex_source_cells:
            degradations.append(
                f"[format compatibility] Worksheet {worksheet.index} has "
                f"{len(worksheet.latex_source_cells)} cell(s) containing complex LaTeX; "
                "the original source is preserved as plain text by policy."
            )

    stats = _statistics(worksheets)
    plan = SheetPlan(
        source_hash=source.source_hash.lower(),
        evidence_dir=str(evidence),
        worksheets=worksheets,
        statistics=stats,
        degradations=degradations,
        warnings=warnings,
    )
    plan.plan_path = str(evidence / "sheet_plan.json")
    plan.manifest_path = str(evidence / "sheet_manifest.json")
    _write_json(Path(plan.plan_path), plan.to_dict())

    manifest = new_manifest(
        route=RouteName.SHEET.value,
        source=source.source_path,
        source_hash=plan.source_hash,
        somark_artifacts=source.to_manifest_dict(),
        dws_cli_version="1.0.57",
        target={
            "title": target.title,
            "profile": "[EXPLICIT]" if target.profile else None,
            "create_only": target.create_only,
            "direct_url": None,
            "worksheets": [
                {
                    "index": worksheet.index,
                    "name": worksheet.name,
                    "source_title": worksheet.source_title,
                    "source_blocks": worksheet.source_blocks,
                }
                for worksheet in worksheets
            ],
        },
    )
    manifest["statistics"] = stats
    manifest["degradations"] = list(degradations)
    manifest["warnings"] = list(warnings)
    manifest["ledger"] = [
        {
            "worksheet_index": worksheet.index,
            "chunk_index": chunk.index,
            "range": chunk.range,
            "content_sha256": chunk.content_sha256,
            "status": chunk.status,
        }
        for worksheet in worksheets
        for chunk in worksheet.value_chunks
    ]
    manifest["readback"] = {"required": True, "performed": False}
    write_manifest_atomic(plan.manifest_path, manifest)
    return plan


__all__ = ["plan_sheet_route"]
