"""Local planning for the SoMark-to-DingTalk AI Table route."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .artifacts import RouteName, RouteTarget, SourceArtifacts
from .errors import redact_sensitive
from .manifest import ManifestStage, new_manifest, write_manifest_atomic
from .aitable_models import AitableAttachmentPlan, AitableFieldPlan, AitablePlan


DWS_CONTRACT_VERSION = "1.0.57"
BUSINESS_TABLE_NAME = "SoMark结构化记录"
MANIFEST_FILENAME = "aitable_route_manifest.json"
FIELD_PLAN_FILENAME = "aitable_field_plan.json"
RECORD_PLAN_FILENAME = "aitable_records.json"
DEGRADATION_PLAN_FILENAME = "aitable_degradation_plan.json"

_HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CELL_FORMULA_RE = re.compile(r"^\s*=.*\b[A-Z]{1,3}\d+\b", re.IGNORECASE)
_PERSON_HINTS = ("负责人", "人员", "成员", "经办人", "审批人", "owner", "assignee", "person", "user")
_LINK_HINTS = ("前置任务", "关联", "依赖", "父记录", "link", "relation", "dependency")
_SELECT_HINTS = ("状态", "优先级", "类别", "分类", "阶段", "status", "priority", "category", "type")
_DATE_HINTS = ("日期", "时间", "date", "time", "开始", "结束", "截止")
_PROGRESS_HINTS = ("进度", "完成率", "progress", "completion")
_IDENTIFIER_HINTS = ("id", "编号", "序号", "代码", "型号", "电话", "手机", "邮编", "账号", "证件")
_READ_ONLY_TYPES = {
    "creator",
    "lastmodifier",
    "createdtime",
    "lastmodifiedtime",
    "formula",
    "lookup",
    "filterup",
    "autonumber",
    "ai",
}
_PERSON_TYPES = {"user", "person", "member"}
_LINK_TYPES = {"unidirectionallink", "bidirectionallink", "association", "relation", "link"}


def _write_json_atomic(path: Path, value: Any) -> Path:
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
            json.dump(redact_sensitive(value), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {label}: {path}") from exc


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_source_hash(source: SourceArtifacts, records_path: Path) -> None:
    if not _HEX_64_RE.fullmatch(source.source_hash):
        raise ValueError("source_hash must be a SHA-256 hex digest")
    hash_target: Path | None = None
    if source.source_path:
        candidate = Path(source.source_path).expanduser().resolve()
        if candidate.is_file():
            hash_target = candidate
    if hash_target is None and source.json_path:
        candidate = Path(source.json_path).expanduser().resolve()
        if candidate.is_file():
            hash_target = candidate
    if hash_target is None:
        hash_target = records_path
    actual = _hash_file(hash_target)
    if actual.casefold() != source.source_hash.casefold():
        raise ValueError(
            f"source hash mismatch for {hash_target}; expected {source.source_hash}, got {actual}"
        )


def _validate_target(target: RouteTarget) -> Path:
    if target.route is not RouteName.AITABLE:
        raise ValueError("target.route must be RouteName.AITABLE")
    if not target.create_only:
        raise ValueError("AI Table route is create-only")
    evidence_dir = Path(target.evidence_dir).expanduser().resolve()
    if evidence_dir.exists() and not evidence_dir.is_dir():
        raise ValueError(f"evidence_dir is not a directory: {evidence_dir}")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir


class _SomarkTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if name == "tr":
            self._row = []
        elif name in {"td", "th"} and self._row is not None:
            self._cell = {
                "parts": [],
                "rowspan": max(1, int(attributes.get("rowspan") or 1)),
                "colspan": max(1, int(attributes.get("colspan") or 1)),
            }
        elif name == "img" and self._cell is not None and attributes.get("src"):
            self._cell["parts"].append(attributes["src"])
        elif name == "br" and self._cell is not None:
            self._cell["parts"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append(self._cell)
            self._cell = None
        elif name == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["parts"].append(data)


def _unwrap_somark_json(value: Any) -> dict[str, Any]:
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


def _table_grid(content: str) -> list[list[str]]:
    parser = _SomarkTableParser()
    try:
        parser.feed(content)
        parser.close()
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid rowspan/colspan in SoMark table: {exc}") from exc
    if not parser.rows or not any(parser.rows):
        raise ValueError("SoMark table contained no rows or cells")
    occupied: set[tuple[int, int]] = set()
    placements: list[tuple[int, int, str]] = []
    row_count = len(parser.rows)
    column_count = 0
    for row_index, row in enumerate(parser.rows):
        column_index = 0
        for cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            text = "".join(cell["parts"]).strip()
            placements.append((row_index, column_index, text))
            for target_row in range(row_index, row_index + cell["rowspan"]):
                for target_column in range(column_index, column_index + cell["colspan"]):
                    occupied.add((target_row, target_column))
            row_count = max(row_count, row_index + cell["rowspan"])
            column_count = max(column_count, column_index + cell["colspan"])
            column_index += cell["colspan"]
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for row_index, column_index, text in placements:
        grid[row_index][column_index] = text
    return grid


def _standard_somark_artifacts(
    source: SourceArtifacts,
    target: RouteTarget,
    json_path: Path,
    value: Mapping[str, Any],
    evidence_dir: Path,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    document = _unwrap_somark_json(value)
    pages = document.get("pages")
    if not isinstance(pages, list):
        raise ValueError("standard SoMark JSON did not contain a decodable pages array")
    candidates: list[dict[str, Any]] = []
    pending_label = ""
    table_number = 0
    for page_index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        page_number = page.get("page_num", page_index)
        for block_index, block in enumerate(page.get("blocks") or []):
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").casefold()
            if block_type in {"title", "table_caption"}:
                text = str(block.get("content") or "").strip()
                if text:
                    pending_label = text
                continue
            if block_type != "table":
                continue
            table_number += 1
            candidates.append(
                {
                    "table_index": table_number,
                    "page": page_number,
                    "idx": block.get("idx", block_index),
                    "title": pending_label,
                    "content": str(block.get("content") or ""),
                }
            )
            pending_label = ""
    if not candidates:
        raise ValueError("SoMark JSON contained no table blocks")
    selected_index = target.table_index
    if selected_index is None and len(candidates) > 1:
        public_candidates = [
            {key: item[key] for key in ("table_index", "page", "idx", "title")}
            for item in candidates
        ]
        raise ValueError(
            "SoMark JSON contains multiple tables; choose --table-index from "
            f"{json.dumps(public_candidates, ensure_ascii=False)}"
        )
    selected_index = selected_index or 1
    selected = next(
        (item for item in candidates if item["table_index"] == selected_index), None
    )
    if selected is None:
        raise ValueError(f"SoMark JSON has no table {selected_index}")
    grid = _table_grid(selected["content"])
    headers = [" ".join(value.split()) for value in grid[0]]
    if not headers or any(not header for header in headers):
        raise ValueError("selected SoMark table has an empty field name")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise ValueError(f"selected SoMark table has duplicate field names: {duplicates!r}")
    records = [
        {header: row[index] for index, header in enumerate(headers) if row[index] != ""}
        for row in grid[1:]
    ]
    if not records:
        raise ValueError("selected SoMark table has no record rows")
    mapping_path = evidence_dir / "somark_generated_field_mapping.json"
    records_path = evidence_dir / "somark_generated_records.json"
    mapping: dict[str, Any] = {
        "source": {
            "kind": "table_rows",
            "table_index": selected_index,
            "header_row": 0,
            "generated_from": str(json_path),
        },
        "fields": OrderedDict((header, {"column": header}) for header in headers),
    }
    records_document: dict[str, Any] = {
        "version": 2,
        "mode": "create_only",
        "source": {"json": str(json_path), "mapping": str(mapping_path)},
        "create_records": records,
    }
    _write_json_atomic(mapping_path, mapping)
    _write_json_atomic(records_path, records_document)
    return records_path, records_document, mapping_path, mapping


def _locate_aitable_inputs(
    source: SourceArtifacts, target: RouteTarget, evidence_dir: Path
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    if not source.json_path:
        raise FileNotFoundError("the AI Table route requires an explicit SoMark JSON artifact")
    candidate = Path(source.json_path).expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"JSON artifact does not exist: {candidate}")
    value = _read_object(candidate, "JSON artifact")
    if isinstance(value.get("create_records"), list):
        mapping_path, mapping = _locate_mapping(source, candidate, value)
        return candidate, value, mapping_path, mapping
    return _standard_somark_artifacts(source, target, candidate, value, evidence_dir)


def _locate_mapping(
    source: SourceArtifacts,
    records_path: Path,
    records_document: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    explicit: list[Path] = []
    source_info = records_document.get("source")
    if isinstance(source_info, Mapping) and isinstance(source_info.get("mapping"), str):
        explicit.append(Path(str(source_info["mapping"])).expanduser())
    for item in source.evidence_files:
        candidate = Path(item).expanduser()
        if candidate.name.casefold() == "field_mapping.json":
            explicit.append(candidate)
    existing = list(
        dict.fromkeys(path.resolve() for path in explicit if path.resolve().is_file())
    )
    if not existing:
        raise FileNotFoundError(
            "prepared records require an explicitly referenced field_mapping.json at records.source.mapping or in evidence_files"
        )
    if len(existing) > 1:
        hashes = {_hash_file(path) for path in existing}
        if len(hashes) > 1:
            raise ValueError("ambiguous field_mapping.json artifacts disagree")
    selected = existing[0]
    return selected, _read_object(selected, "field mapping")


def _degradation(category: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return redact_sensitive(
        {"category": category, "code": code, "message": message, **details}
    )


def _normalise_type(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def normalize_date_value(value: Any, timezone: str = "Asia/Shanghai") -> str:
    """Convert SoMark dates/milliseconds to DingTalk-safe date strings."""

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown business timezone: {timezone}") from exc

    parsed: datetime
    if isinstance(value, bool):
        raise ValueError(f"boolean is not a date: {value!r}")
    if isinstance(value, (int, float, Decimal)):
        milliseconds = Decimal(str(value))
        if not milliseconds.is_finite():
            raise ValueError(f"non-finite timestamp: {value!r}")
        try:
            parsed = datetime.fromtimestamp(float(milliseconds / Decimal(1000)), tz=zone)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"invalid millisecond timestamp: {value!r}") from exc
    elif isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("blank date value")
        if re.fullmatch(r"[+-]?\d{11,17}", text):
            return normalize_date_value(Decimal(text), timezone)
        normalised = re.sub(r"\s*年\s*", "-", text)
        normalised = re.sub(r"\s*月\s*", "-", normalised)
        normalised = re.sub(r"\s*日\s*$", "", normalised)
        if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", normalised):
            normalised = normalised.replace("/", "-").replace(".", "-")
            try:
                return date.fromisoformat(normalised).isoformat()
            except ValueError as exc:
                raise ValueError(f"invalid date: {value!r}") from exc
        try:
            parsed = datetime.fromisoformat(normalised.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid date: {value!r}") from exc
    else:
        raise ValueError(f"unsupported date value: {value!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    if parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0:
        return parsed.date().isoformat()
    return parsed.isoformat(timespec="seconds")


def _number_value(value: Any, field_name: str) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"field {field_name!r} contains a boolean, not a number")
    if isinstance(value, (int, float)):
        number = Decimal(str(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip().replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"field {field_name!r} contains a non-number: {value!r}") from exc
    else:
        raise ValueError(f"field {field_name!r} contains a non-number: {value!r}")
    if not number.is_finite():
        raise ValueError(f"field {field_name!r} contains a non-finite number")
    integral = number.to_integral_value()
    return int(integral) if number == integral else float(number)


def _all_numbers(values: Iterable[Any], field_name: str) -> bool:
    try:
        for value in values:
            if not _is_empty(value):
                _number_value(value, field_name)
    except ValueError:
        return False
    return True


def _all_dates(values: Iterable[Any], timezone: str) -> bool:
    try:
        for value in values:
            if not _is_empty(value):
                normalize_date_value(value, timezone)
    except ValueError:
        return False
    return True


def _progress_value(value: Any, field_name: str) -> int | float:
    """Convert common source percentages to DingTalk's native 0..1 progress value."""

    percent_literal = isinstance(value, str) and value.strip().endswith("%")
    raw = value.strip()[:-1] if percent_literal else value
    number = Decimal(str(_number_value(raw, field_name)))
    if percent_literal:
        number /= Decimal(100)
    elif number > 1 and number <= 100:
        number /= Decimal(100)
    if number < 0 or number > 1:
        raise ValueError(f"field {field_name!r} contains progress outside 0..100%: {value!r}")
    integral = number.to_integral_value()
    return int(integral) if number == integral else float(number)


def _has_progress_value(values: Iterable[Any], field_name: str) -> bool:
    for value in values:
        if _is_empty(value):
            continue
        try:
            _progress_value(value, field_name)
            return True
        except ValueError:
            continue
    return False


def _number_formatter(values: Iterable[Any], field_name: str) -> str:
    parsed: list[int | float] = []
    for value in values:
        if _is_empty(value):
            continue
        try:
            parsed.append(_number_value(value, field_name))
        except ValueError:
            continue
    return "INT" if parsed and all(float(value).is_integer() for value in parsed) else "FLOAT_4"


def _first_seen_options(values: Iterable[Any]) -> list[str]:
    options: list[str] = []
    for value in values:
        if _is_empty(value):
            continue
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return []
        name = str(value)
        if name not in options:
            options.append(name)
    return options


def _spec_options(spec: Mapping[str, Any]) -> list[str]:
    config = spec.get("config")
    raw = config.get("options") if isinstance(config, Mapping) else spec.get("options")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, Mapping) else item
        if not isinstance(name, str) or not name:
            return []
        if name not in names:
            names.append(name)
    return names


def _infer_field(
    name: str,
    spec: Mapping[str, Any],
    values: list[Any],
    order: int,
    timezone: str,
) -> AitableFieldPlan:
    explicit_raw = spec.get("type")
    explicit = _normalise_type(explicit_raw)
    lowered_name = name.casefold()
    non_empty = [value for value in values if not _is_empty(value)]
    target_type = "text"
    config: dict[str, Any] = {}
    downgrade: str | None = None
    record_writable = True

    if explicit in _READ_ONLY_TYPES:
        target_type = "text"
        downgrade = f"source type {explicit_raw!r} is read-only/unsupported for record writes; preserved as text"
    elif explicit in _PERSON_TYPES or any(hint in lowered_name for hint in _PERSON_HINTS):
        reliable = bool(non_empty) and all(
            isinstance(value, list)
            and all(
                isinstance(item, Mapping) and item.get("userId") and item.get("corpId")
                for item in value
            )
            for value in non_empty
        )
        if reliable and explicit in _PERSON_TYPES:
            target_type = "user"
        else:
            target_type = "text"
            downgrade = "person names lack reliable userId + corpId; IDs were not guessed"
    elif explicit in _LINK_TYPES or any(hint in lowered_name for hint in _LINK_HINTS):
        target_type = "text"
        downgrade = "relation labels lack a safe target recordId mapping; second-phase relation upgrade is disabled"
    elif explicit == "attachment":
        target_type = "attachment"
        record_writable = False
        downgrade = "attachment binary upload is unsupported_in_dws_only"
    elif explicit in {"date", "datetime"}:
        target_type = "date"
        config = {"formatter": "YYYY-MM-DD"}
    elif explicit == "progress" or (
        any(hint in lowered_name for hint in _PROGRESS_HINTS)
        and _has_progress_value(non_empty, name)
    ):
        target_type = "progress"
    elif explicit in {"number", "currency", "rating"}:
        target_type = explicit
        config = {"formatter": _number_formatter(values, name)} if explicit != "rating" else {}
    elif explicit in {"singleselect", "select", "enum"}:
        options = _spec_options(spec) or _first_seen_options(values)
        if options:
            target_type = "singleSelect"
            config = {"options": [{"name": option} for option in options]}
        else:
            downgrade = "finite select options could not be proven; values were preserved as text"
    elif explicit in {"text", "richtext", "url", "telephone", "email", "barcode", "idcard"}:
        target_type = "text"
        if explicit not in {"text", ""}:
            downgrade = f"source type {explicit_raw!r} was conservatively preserved as text"
    elif explicit:
        target_type = "text"
        downgrade = f"unsupported source type {explicit_raw!r} was conservatively preserved as text"
    elif any(hint in lowered_name for hint in _SELECT_HINTS):
        options = _first_seen_options(values)
        if 1 <= len(options) <= 30:
            target_type = "singleSelect"
            config = {"options": [{"name": option} for option in options]}
        else:
            downgrade = "finite select options could not be proven; values were preserved as text"
    elif any(hint in lowered_name for hint in _DATE_HINTS):
        target_type = "date"
        config = {"formatter": "YYYY-MM-DD"}
    elif non_empty and not any(hint in lowered_name for hint in _IDENTIFIER_HINTS) and _all_numbers(non_empty, name):
        target_type = "number"
        config = {"formatter": _number_formatter(values, name)}

    if order == 0 and target_type != "text":
        previous = target_type
        target_type = "text"
        config = {}
        record_writable = True
        downgrade = (
            f"primary field must be usable text; downgraded from {previous}"
            + (f"; {downgrade}" if downgrade else "")
        )

    return AitableFieldPlan(
        source_name=name,
        source_type=str(explicit_raw or "inferred"),
        target_type=target_type,
        config=config,
        create_order=order,
        primary=order == 0,
        record_writable=record_writable,
        downgrade_reason=downgrade,
    )


def _normalise_field_value(
    value: Any,
    field: AitableFieldPlan,
    timezone: str,
) -> Any:
    if value is None:
        return None
    if field.target_type == "date":
        return normalize_date_value(value, timezone)
    if field.target_type in {"number", "currency", "rating"}:
        return _number_value(value, field.source_name)
    if field.target_type == "progress":
        return _progress_value(value, field.source_name)
    if field.target_type == "singleSelect":
        return str(value)
    if field.target_type in {"user", "attachment"}:
        return value
    return _stringify(value)


def _route_signals(
    records_document: Mapping[str, Any],
    mapping: Mapping[str, Any],
    records: list[Mapping[str, Any]],
    records_path: Path,
) -> list[str]:
    reasons: list[str] = []
    source_config = mapping.get("source")
    if not isinstance(source_config, Mapping) or source_config.get("kind", "table_rows") != "table_rows":
        reasons.append("source is not a stable table_rows record model")
    text = json.dumps(
        {"records_meta": {key: value for key, value in records_document.items() if key != "create_records"}, "mapping": mapping},
        ensure_ascii=False,
    ).casefold()
    source_info = records_document.get("source")
    if isinstance(source_info, Mapping) and isinstance(source_info.get("json"), str):
        raw_path = Path(str(source_info["json"])).expanduser()
        if not raw_path.is_absolute():
            raw_path = records_path.parent / raw_path
        raw_path = raw_path.resolve()
        if raw_path.is_file() and raw_path != records_path:
            try:
                text += "\n" + raw_path.read_text(encoding="utf-8-sig").casefold()
            except OSError:
                pass
    signal_groups = {
        "merged cells detected": ("merged_cells", "mergedcells", "rowspan", "colspan"),
        "repeated headers detected": ("repeated_header", "repeatedheader", "分页表头", "重复表头"),
        "strong visual-style requirement detected": ("strong_style", "layout_preserving", "preserve_style", "视觉样式"),
        "coordinate formula model detected": ("coordinate_formula", "cell_formula", "坐标公式"),
    }
    for reason, signals in signal_groups.items():
        if any(signal in text for signal in signals):
            reasons.append(reason)
    for record in records:
        for value in record.values():
            if isinstance(value, str) and _CELL_FORMULA_RE.search(value):
                reasons.append("coordinate formula value detected")
                return list(dict.fromkeys(reasons))
    return list(dict.fromkeys(reasons))


def _attachment_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _attachment_plan(
    field_name: str,
    record_index: int,
    value: Any,
    records_dir: Path,
) -> AitableAttachmentPlan:
    path_value: str | None = None
    if isinstance(value, str):
        path_value = value
    elif isinstance(value, Mapping) and isinstance(value.get("path"), str):
        path_value = str(value["path"])
    path: Path | None = None
    if path_value:
        candidate = Path(path_value).expanduser()
        if not candidate.is_absolute():
            candidate = records_dir / candidate
        candidate = candidate.resolve()
        if candidate.is_file():
            path = candidate
    file_name = path.name if path else (
        str(value.get("fileName")) if isinstance(value, Mapping) and value.get("fileName") else None
    )
    size = path.stat().st_size if path else None
    mime_type = mimetypes.guess_type(file_name or "")[0]
    return AitableAttachmentPlan(
        field_name=field_name,
        record_index=record_index,
        source_value=value,
        local_path=str(path) if path else None,
        file_name=file_name,
        size=size,
        mime_type=mime_type,
    )


def _source_manifest_artifacts(
    source: SourceArtifacts,
    records_path: Path,
    mapping_path: Path,
    field_plan_path: Path,
    record_plan_path: Path,
    degradation_path: Path,
) -> dict[str, Any]:
    return {
        "records": str(records_path),
        "field_mapping": str(mapping_path),
        "raw_json": source.json_path,
        "markdown": source.markdown_path,
        "assets": source.assets_dir,
        "evidence_files": list(source.evidence_files),
        "field_plan": str(field_plan_path),
        "record_plan": str(record_plan_path),
        "degradation_plan": str(degradation_path),
    }


def plan_aitable_route(
    source: SourceArtifacts,
    target: RouteTarget,
    *,
    timezone: str = "Asia/Shanghai",
) -> AitablePlan:
    """Validate explicit input artifacts and write a deterministic local plan."""

    evidence_dir = _validate_target(target)
    records_path, records_document, mapping_path, mapping = _locate_aitable_inputs(
        source, target, evidence_dir
    )
    _validate_source_hash(source, records_path)

    raw_records = records_document.get("create_records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("records.json create_records must be a non-empty array")
    if not all(isinstance(item, Mapping) for item in raw_records):
        raise ValueError("each create_records entry must be an object")
    records = [dict(item) for item in raw_records]

    mapped_fields = mapping.get("fields")
    if mapped_fields is None:
        mapped_fields = {}
    if not isinstance(mapped_fields, Mapping):
        raise ValueError("field_mapping.json fields must be an object when present")
    fields: OrderedDict[str, Mapping[str, Any]] = OrderedDict()
    for name, spec in mapped_fields.items():
        if not isinstance(name, str) or not name:
            raise ValueError("field names must be non-empty strings")
        if not isinstance(spec, Mapping):
            raise ValueError(f"field mapping for {name!r} must be an object")
        fields[name] = spec
    for record in records:
        for name in record:
            if not isinstance(name, str) or not name:
                raise ValueError("record field names must be non-empty strings")
            fields.setdefault(name, {})
    if not fields:
        raise ValueError("no fields were found in field_mapping.json or records.json")
    field_names = list(fields)
    if not all(isinstance(name, str) and name for name in field_names):
        raise ValueError("field names must be non-empty strings")
    if len(set(field_names)) != len(field_names):
        raise ValueError("field names must be unique")
    route_reasons = _route_signals(records_document, mapping, records, records_path)
    field_plans: list[AitableFieldPlan] = []
    for order, name in enumerate(field_names):
        values = [record[name] for record in records if name in record]
        field_plans.append(
            _infer_field(name, fields[name], values, order, timezone)
        )

    normalized_records: list[OrderedDict[str, Any]] = []
    date_conversions = 0
    missing_cells = 0
    explicit_nulls = 0
    empty_strings = 0
    progress_conversions = 0
    conversion_degradations: list[dict[str, Any]] = []
    for record_index, record in enumerate(records):
        normalized: OrderedDict[str, Any] = OrderedDict()
        for field in field_plans:
            if field.source_name not in record:
                missing_cells += 1
                continue
            value = record[field.source_name]
            if value is None:
                explicit_nulls += 1
                continue
            elif isinstance(value, str) and value.strip() == "":
                empty_strings += 1
                continue
            try:
                converted = _normalise_field_value(value, field, timezone)
            except (TypeError, ValueError, OverflowError) as exc:
                conversion_degradations.append(
                    _degradation(
                        "cell_conversion",
                        "cell_omitted",
                        "cell value could not be converted to the selected DingTalk field type; the cell was left blank and conversion continued",
                        record_index=record_index,
                        field_name=field.source_name,
                        target_type=field.target_type,
                        reason=str(exc),
                    )
                )
                continue
            if field.target_type == "date" and converted != value:
                date_conversions += 1
            if field.target_type == "progress" and converted != value:
                progress_conversions += 1
            normalized[field.source_name] = converted
        normalized_records.append(normalized)

    attachment_plans: list[AitableAttachmentPlan] = []
    for field in field_plans:
        if field.target_type != "attachment":
            continue
        for record_index, record in enumerate(records):
            if field.source_name not in record:
                continue
            for value in _attachment_values(record[field.source_name]):
                attachment_plans.append(
                    _attachment_plan(field.source_name, record_index, value, records_path.parent)
                )

    degradations: list[dict[str, Any]] = []
    for field in field_plans:
        if field.downgrade_reason:
            degradations.append(
                _degradation(
                    "field_mapping",
                    "field_downgraded",
                    field.downgrade_reason,
                    field_name=field.source_name,
                    source_type=field.source_type,
                    target_type=field.target_type,
                )
            )
    degradations.extend(conversion_degradations)
    if route_reasons:
        degradations.append(
            _degradation(
                "routing",
                "sheet_route_recommended",
                "the sheet route may preserve layout more faithfully; the explicit AI Table route will continue with physical-grid and text degradation",
                reasons=route_reasons,
            )
        )
    if attachment_plans:
        degradations.append(
            _degradation(
                "attachment",
                "unsupported_in_dws_only",
                "DWS v1.0.57 prepares upload credentials but does not upload binary content; no HTTP call will be made",
                count=len(attachment_plans),
            )
        )

    statistics = {
        "field_count": len(field_plans),
        "record_count": len(normalized_records),
        "field_batch_count": (len(field_plans) + 14) // 15,
        "record_batch_count": (len(normalized_records) + 29) // 30,
        "date_conversion_count": date_conversions,
        "progress_conversion_count": progress_conversions,
        "cell_conversion_failure_count": len(conversion_degradations),
        "empty_value_count": missing_cells + explicit_nulls + empty_strings,
        "missing_cell_count": missing_cells,
        "explicit_null_count": explicit_nulls,
        "empty_string_count": empty_strings,
        "degraded_field_count": sum(field.downgrade_reason is not None for field in field_plans),
        "attachment_plan_count": len(attachment_plans),
    }

    field_plan_path = evidence_dir / FIELD_PLAN_FILENAME
    record_plan_path = evidence_dir / RECORD_PLAN_FILENAME
    degradation_path = evidence_dir / DEGRADATION_PLAN_FILENAME
    manifest_path = evidence_dir / MANIFEST_FILENAME
    _write_json_atomic(
        field_plan_path,
        {
            "schema_version": 1,
            "route": RouteName.AITABLE.value,
            "source_field_order": field_names,
            "fields": [field.to_safe_dict() for field in field_plans],
            "creation_payload": [field.creation_payload() for field in field_plans],
        },
    )
    _write_json_atomic(
        record_plan_path,
        {
            "schema_version": 1,
            "requires_field_ids": True,
            "source_field_order": field_names,
            "records": [
                {"source_index": index, "cells_by_source_field": dict(record)}
                for index, record in enumerate(normalized_records)
            ],
        },
    )
    _write_json_atomic(
        degradation_path,
        {
            "schema_version": 1,
            "route_eligible": True,
            "recommended_route": RouteName.AITABLE.value if not route_reasons else RouteName.SHEET.value,
            "routing_reasons": route_reasons,
            "explicit_aitable_route_honored": True,
            "degradations": degradations,
            "attachments": [item.to_safe_dict() for item in attachment_plans],
        },
    )

    artifacts = _source_manifest_artifacts(
        source,
        records_path,
        mapping_path,
        field_plan_path,
        record_plan_path,
        degradation_path,
    )
    manifest = new_manifest(
        route=RouteName.AITABLE.value,
        source=source.source_path or str(records_path),
        source_hash=source.source_hash.casefold(),
        somark_artifacts=artifacts,
        dws_cli_version=DWS_CONTRACT_VERSION,
        target={
            "baseId": None,
            "tableId": None,
            "title": target.title,
            "table_name": BUSINESS_TABLE_NAME,
            "root_url": None,
            "direct_url": None,
        },
    )
    manifest["statistics"] = statistics
    manifest["degradations"] = degradations
    manifest["warnings"] = (
        [
            "Sheet route may preserve layout more faithfully; the explicitly selected AI Table route will still be written using physical-grid and text degradation"
        ]
        if route_reasons
        else []
    )
    manifest["ledger"].append(
        {
            "operation": "plan_aitable_route",
            "stage": ManifestStage.PENDING.value,
            "remote_write": False,
            "validation": {
                "records_loaded": len(normalized_records),
                "fields_loaded": len(field_plans),
                "source_hash_matched": True,
                "route_eligible": True,
                "route_recommended": not route_reasons,
            },
        }
    )
    write_manifest_atomic(manifest_path, manifest)

    return AitablePlan(
        title=target.title,
        table_name=BUSINESS_TABLE_NAME,
        records_path=str(records_path),
        mapping_path=str(mapping_path),
        evidence_dir=str(evidence_dir),
        manifest_path=str(manifest_path),
        field_plan_path=str(field_plan_path),
        record_plan_path=str(record_plan_path),
        degradation_plan_path=str(degradation_path),
        field_plans=tuple(field_plans),
        normalized_records=tuple(normalized_records),
        attachment_plans=tuple(attachment_plans),
        route_eligible=True,
        recommended_route=RouteName.AITABLE.value if not route_reasons else RouteName.SHEET.value,
        routing_reasons=tuple(route_reasons),
        statistics=statistics,
        degradations=tuple(degradations),
        warnings=tuple(manifest["warnings"]),
    )


__all__ = [
    "BUSINESS_TABLE_NAME",
    "DEGRADATION_PLAN_FILENAME",
    "DWS_CONTRACT_VERSION",
    "FIELD_PLAN_FILENAME",
    "MANIFEST_FILENAME",
    "RECORD_PLAN_FILENAME",
    "normalize_date_value",
    "plan_aitable_route",
]
