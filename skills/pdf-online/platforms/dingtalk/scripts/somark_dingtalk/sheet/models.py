"""Typed plans for faithful SoMark table reconstruction and DingTalk writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceRowMapping:
    source_page: int
    source_block: int
    source_row: int
    target_row: int


@dataclass(frozen=True)
class MergePlan:
    range: str
    anchor: str
    source_page: int
    source_block: int


@dataclass(frozen=True)
class StylePlan:
    range: str
    options: dict[str, Any]


@dataclass(frozen=True)
class DimensionPlan:
    dimension: str
    start_index: str
    length: int
    pixel_size: int


@dataclass(frozen=True)
class ImagePlan:
    cell: str
    source_reference: str
    local_path: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class ValueChunk:
    index: int
    start_row: int
    end_row: int
    range: str
    cell_count: int
    values_path: str
    content_sha256: str
    status: str = "pending"


@dataclass
class WorksheetPlan:
    index: int
    name: str
    source_title: str
    rows: list[list[str]]
    source_blocks: list[dict[str, int]]
    row_mappings: list[SourceRowMapping]
    value_chunks: list[ValueChunk] = field(default_factory=list)
    styles: list[StylePlan] = field(default_factory=list)
    merges: list[MergePlan] = field(default_factory=list)
    dimensions: list[DimensionPlan] = field(default_factory=list)
    images: list[ImagePlan] = field(default_factory=list)
    duplicate_header_rows: list[int] = field(default_factory=list)
    fully_empty_rows: list[int] = field(default_factory=list)
    latex_unicode_cells: list[str] = field(default_factory=list)
    latex_source_cells: list[str] = field(default_factory=list)
    literal_formula_cells: list[str] = field(default_factory=list)
    merge_degradations: list[dict[str, Any]] = field(default_factory=list)
    csv_path: str | None = None
    text_policy_json_path: str | None = None
    style_json_path: str | None = None
    merge_json_path: str | None = None
    dimension_json_path: str | None = None
    image_json_path: str | None = None
    route_boundary: str = "layout_only_plain_text_sheet"

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def cell_count(self) -> int:
        return self.row_count * self.column_count


@dataclass
class SheetPlan:
    source_hash: str
    evidence_dir: str
    worksheets: list[WorksheetPlan]
    statistics: dict[str, Any]
    degradations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    plan_path: str | None = None
    manifest_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def primary(self) -> WorksheetPlan:
        if not self.worksheets:
            raise ValueError("sheet plan contains no worksheets")
        return self.worksheets[0]

    def existing_manifest_path(self) -> Path | None:
        if not self.manifest_path:
            return None
        path = Path(self.manifest_path)
        return path if path.is_file() else None


__all__ = [
    "DimensionPlan",
    "ImagePlan",
    "MergePlan",
    "SheetPlan",
    "SourceRowMapping",
    "StylePlan",
    "ValueChunk",
    "WorksheetPlan",
]
