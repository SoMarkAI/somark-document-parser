"""Prepare SoMark table rows and create Feishu Bitable records.

``prepare`` maps selected SoMark table rows to a reviewable record payload.
``auto-prepare`` derives headers from SoMark JSON and prepares records without
opening the original file or running a second OCR pass.
``create-base`` creates a new empty Base with fields derived from the mapping.
``start-create`` writes the first record batch and only then exposes the preview URL.
``create`` appends every prepared record to an existing Feishu Base table.
``run`` performs both steps while retaining the generated payload for audit.

This adapter intentionally does not deduplicate repeated uploads. It does not
create an internal Source Key field, read existing records, update records, or
maintain a local record-ID map.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from prepare_sheets_payload import parse_table, unwrap_somark_json


BATCH_SIZE = 200
BASE_FIELD_TYPES = {
    "text": "text",
    "number": "number",
    "date": "datetime",
}
IDENTIFIER_HEADER_HINTS = (
    "id",
    "编号",
    "序号",
    "代码",
    "型号",
    "电话",
    "手机",
    "邮编",
    "账号",
    "证件",
)


class PayloadValidationError(ValueError):
    """Raised when a prepared Bitable payload is invalid."""


class MappingValidationError(ValueError):
    """Raised when a SoMark-to-Bitable field mapping is invalid."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingValidationError(f"unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise MappingValidationError(f"{label} must be a JSON object: {path}")
    return value


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalise_header(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_field_names(headers: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    used: set[str] = set()
    names: list[str] = []
    repairs: list[dict[str, Any]] = []
    for column_index, source_header in enumerate(headers):
        base = source_header or "文本"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}{suffix}"
            suffix += 1
        used.add(candidate)
        names.append(candidate)
        if candidate != source_header:
            repairs.append(
                {
                    "column_index": column_index,
                    "source_header": source_header,
                    "field_name": candidate,
                    "reason": "empty_header" if not source_header else "duplicate_header",
                }
            )
    return names, repairs


def _parse_date(value: Any, field_name: str) -> int:
    text = str(value).strip()
    normalised = re.sub(r"\s*年\s*", "-", text)
    normalised = re.sub(r"\s*月\s*", "-", normalised)
    normalised = re.sub(r"\s*日\s*$", "", normalised)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalised.replace("Z", "+00:00"))
    except ValueError:
        for candidate, date_format in (
            (normalised, "%Y-%m-%d"),
            (text, "%Y/%m/%d"),
            (text, "%Y.%m.%d"),
        ):
            try:
                parsed = datetime.strptime(candidate, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        raise MappingValidationError(
            f"field {field_name!r} expected an ISO or Chinese date, got {value!r}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(parsed.timestamp() * 1000)


def _coerce_value(value: Any, value_type: str, field_name: str) -> Any:
    if value_type == "text":
        return str(value).strip()
    if value_type == "number":
        text = str(value).strip().replace(",", "")
        try:
            number = float(text)
        except ValueError as exc:
            raise MappingValidationError(
                f"field {field_name!r} expected a number, got {value!r}"
            ) from exc
        return int(number) if number.is_integer() else number
    if value_type == "date":
        return _parse_date(value, field_name)
    raise MappingValidationError(
        f"field {field_name!r} has unsupported type {value_type!r}; "
        "use text, number, or date"
    )


def base_fields_from_mapping(mapping_path: Path) -> list[dict[str, str]]:
    """Build the initial Feishu Base field schema from a validated mapping."""

    mapping = _read_json_object(mapping_path, "mapping")
    fields = mapping.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise MappingValidationError("mapping fields must be a non-empty object")
    base_fields: list[dict[str, str]] = []
    for field_name, spec in fields.items():
        if not isinstance(field_name, str) or not field_name.strip():
            raise MappingValidationError("mapping field names must be non-empty strings")
        if not isinstance(spec, dict):
            raise MappingValidationError(f"field {field_name!r} must be an object")
        value_type = spec.get("type", "text")
        if not isinstance(value_type, str) or value_type not in BASE_FIELD_TYPES:
            raise MappingValidationError(
                f"field {field_name!r} has unsupported type {value_type!r}; "
                "use text, number, or date"
            )
        base_fields.append(
            {"name": field_name.strip(), "type": BASE_FIELD_TYPES[value_type]}
        )
    return base_fields


def _column_index(headers: list[str], column_spec: Any, field_name: str) -> int:
    aliases = [column_spec] if isinstance(column_spec, str) else column_spec
    if not isinstance(aliases, list) or not aliases or not all(
        isinstance(alias, str) and alias.strip() for alias in aliases
    ):
        raise MappingValidationError(
            f"field {field_name!r} column must be a non-empty string or alias list"
        )
    for alias in aliases:
        matches = [index for index, header in enumerate(headers) if header == alias.strip()]
        if len(matches) > 1:
            raise MappingValidationError(
                f"field {field_name!r} matched duplicate source column {alias!r}"
            )
        if matches:
            return matches[0]
    raise MappingValidationError(
        f"field {field_name!r} could not find source column; tried {aliases!r}"
    )


def _iter_table_blocks(document: dict[str, Any]) -> Iterable[tuple[int, int, dict[str, Any]]]:
    table_index = 0
    for page_index, page in enumerate(document.get("pages", [])):
        if not isinstance(page, dict):
            continue
        page_number = page.get("page_num", page_index + 1)
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            page_number = page_index + 1
        for block in page.get("blocks", []):
            if not isinstance(block, dict) or block.get("type") != "table":
                continue
            table_index += 1
            yield table_index, page_number, block


def _infer_column_type(header: str, values: list[Any]) -> str:
    non_empty = [value for value in values if not _is_empty(value)]
    if not non_empty:
        return "text"

    date_values = True
    for value in non_empty:
        try:
            _parse_date(value, header)
        except MappingValidationError:
            date_values = False
            break
    if date_values:
        return "date"

    lowered_header = header.casefold()
    if any(hint in lowered_header for hint in IDENTIFIER_HEADER_HINTS):
        return "text"
    for value in non_empty:
        text = str(value).strip().replace(",", "")
        if re.fullmatch(r"[+-]?0\d+", text):
            return "text"
        try:
            float(text)
        except ValueError:
            return "text"
    return "number"


def mapping_from_somark_json(
    source_json: Path,
    *,
    table_index: int | None = None,
    header_row: int = 0,
    data_start_row: int | None = None,
    skip_empty_rows: bool = False,
) -> dict[str, Any]:
    """Create a field mapping directly from a SoMark JSON table block."""

    if table_index is not None and table_index < 1:
        raise MappingValidationError("table_index must be a positive integer")
    if header_row < 0:
        raise MappingValidationError("header_row must be a non-negative integer")
    if data_start_row is None:
        data_start_row = header_row + 1
    if data_start_row <= header_row:
        raise MappingValidationError("data_start_row must be after header_row")

    document = unwrap_somark_json(_read_json_object(source_json, "SoMark JSON"))
    table_blocks = list(_iter_table_blocks(document))
    if table_index is None:
        if len(table_blocks) > 1:
            candidates = [
                {
                    "table_index": current_index,
                    "page": page_number,
                    "idx": block.get("idx"),
                }
                for current_index, page_number, block in table_blocks
            ]
            raise MappingValidationError(
                "SoMark JSON contains multiple tables; choose --table-index from "
                f"{json.dumps(candidates, ensure_ascii=False)}"
            )
        if table_blocks:
            table_index = table_blocks[0][0]
    selected = next(
        (block for current_index, _, block in table_blocks if current_index == table_index),
        None,
    )
    if selected is None:
        raise MappingValidationError(f"SoMark JSON has no table {table_index}")
    source = selected.get("content")
    if not isinstance(source, str) or not source.strip():
        raise MappingValidationError(f"table {table_index} has no HTML content")
    grid, _, _ = parse_table(source, text_mode="raw")
    if header_row >= len(grid):
        raise MappingValidationError(
            f"table {table_index} has no configured header row {header_row + 1}"
        )

    headers = [_normalise_header(value) for value in grid[header_row]]
    if not headers:
        raise MappingValidationError(f"table {table_index} header row has no columns")
    field_names, header_repairs = _safe_field_names(headers)

    fields: dict[str, dict[str, Any]] = {}
    for column_index, (header, field_name) in enumerate(zip(headers, field_names)):
        values = [
            row[column_index] if column_index < len(row) else None
            for row in grid[data_start_row:]
        ]
        value_type = _infer_column_type(field_name, values)
        spec: dict[str, Any] = {"column": header}
        if field_name != header or headers.count(header) > 1:
            spec.update(
                {
                    "column_index": column_index,
                    "source_header": header,
                }
            )
        if value_type != "text":
            spec["type"] = value_type
        fields[field_name] = spec

    return {
        "source": {
            "kind": "table_rows",
            "table_index": table_index,
            "header_row": header_row,
            "data_start_row": data_start_row,
            "skip_empty_rows": skip_empty_rows,
        },
        "fields": fields,
        "warnings": [
            "The SoMark header required collision-safe field-name repairs; review the "
            "mapping before creating the Base. All original cell values remain available."
        ]
        if header_repairs
        else [],
        "header_repairs": header_repairs,
    }


def prepare_payload(
    source_json: Path,
    mapping_path: Path,
    *,
    source_file: Path | None = None,
) -> dict[str, Any]:
    """Map selected SoMark HTML table rows to a create-only Bitable payload."""

    mapping = _read_json_object(mapping_path, "mapping")
    document = unwrap_somark_json(_read_json_object(source_json, "SoMark JSON"))
    if not isinstance(document.get("pages"), list):
        raise MappingValidationError("SoMark JSON contained no pages")
    if source_file is not None and not source_file.is_file():
        raise MappingValidationError(f"source file not found: {source_file}")

    source_config = mapping.get("source", {})
    if not isinstance(source_config, dict):
        raise MappingValidationError("mapping source must be an object")
    if source_config.get("kind", "table_rows") != "table_rows":
        raise MappingValidationError("only source.kind=table_rows is supported")
    header_row = source_config.get("header_row", 0)
    data_start_row = source_config.get("data_start_row")
    table_selector = source_config.get("table_index", "all")
    skip_empty_rows = source_config.get("skip_empty_rows", False)
    if not isinstance(header_row, int) or header_row < 0:
        raise MappingValidationError("source.header_row must be a non-negative integer")
    if data_start_row is None:
        data_start_row = header_row + 1
    if not isinstance(data_start_row, int) or data_start_row <= header_row:
        raise MappingValidationError("source.data_start_row must be after header_row")
    if table_selector != "all" and (
        not isinstance(table_selector, int) or table_selector < 1
    ):
        raise MappingValidationError("source.table_index must be 'all' or a 1-based integer")
    if not isinstance(skip_empty_rows, bool):
        raise MappingValidationError("source.skip_empty_rows must be true or false")

    fields = mapping.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise MappingValidationError("mapping fields must be a non-empty object")
    if not all(isinstance(name, str) and name.strip() for name in fields):
        raise MappingValidationError("mapping field names must be non-empty strings")

    records: list[dict[str, Any]] = []
    selected_tables = 0
    for table_index, page_number, block in _iter_table_blocks(document):
        if table_selector != "all" and table_index != table_selector:
            continue
        source = block.get("content")
        if not isinstance(source, str) or not source.strip():
            raise MappingValidationError(
                f"table {table_index} on page {page_number} has no HTML content"
            )
        grid, _, _ = parse_table(source, text_mode="raw")
        if header_row >= len(grid):
            raise MappingValidationError(
                f"table {table_index} has no configured header row {header_row + 1}"
            )
        headers = [_normalise_header(value) for value in grid[header_row]]
        if not any(headers):
            raise MappingValidationError(f"table {table_index} header row is empty")

        column_indexes: dict[str, int | None] = {}
        for field_name, spec in fields.items():
            if not isinstance(spec, dict):
                raise MappingValidationError(f"field {field_name!r} must be an object")
            configured_index = spec.get("column_index")
            if configured_index is not None:
                if (
                    not isinstance(configured_index, int)
                    or configured_index < 0
                    or configured_index >= len(headers)
                ):
                    raise MappingValidationError(
                        f"field {field_name!r} column_index is outside the table header"
                    )
                column_indexes[field_name] = configured_index
            else:
                column_indexes[field_name] = (
                    _column_index(headers, spec["column"], field_name)
                    if "column" in spec
                    else None
                )
            if column_indexes[field_name] is None and "value" not in spec:
                raise MappingValidationError(
                    f"field {field_name!r} needs column or value in mapping"
                )

        selected_tables += 1
        last_values: dict[str, Any] = {}
        for row_index in range(data_start_row, len(grid)):
            row = grid[row_index]
            row_is_empty = all(_is_empty(value) for value in row)
            if row_is_empty:
                if skip_empty_rows:
                    continue
                # Preserve source spacer rows without applying fill-down or defaults.
                records.append({})
                continue
            record: dict[str, Any] = {}
            for field_name, spec in fields.items():
                assert isinstance(spec, dict)
                column_index = column_indexes[field_name]
                value = spec.get("value") if column_index is None else (
                    row[column_index] if column_index < len(row) else None
                )
                if _is_empty(value) and spec.get("fill_down"):
                    value = last_values.get(field_name)
                if _is_empty(value) and "default" in spec:
                    value = spec["default"]
                if _is_empty(value):
                    continue
                value_type = spec.get("type", "text")
                if not isinstance(value_type, str):
                    raise MappingValidationError(f"field {field_name!r} type must be a string")
                converted = _coerce_value(value, value_type, field_name)
                record[field_name] = converted
                last_values[field_name] = converted
            records.append(record)

    if selected_tables == 0:
        raise MappingValidationError("mapping selected no SoMark table blocks")
    if not records:
        raise MappingValidationError("mapping produced no Bitable records")
    return {
        "version": 2,
        "mode": "create_only",
        "source": {
            "json": str(source_json.resolve()),
            "source_file": str(source_file.resolve()) if source_file else None,
            "mapping": str(mapping_path.resolve()),
        },
        "create_records": records,
    }


def write_prepared_payload(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def chunks(items: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def read_payload(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadValidationError(f"unable to read payload: {path}") from exc
    records = data.get("create_records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise PayloadValidationError("payload must contain a create_records array")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise PayloadValidationError(f"create_records[{index}] must be an object")
    return records


def write_json_file(value: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_base_target_details(path: Path) -> dict[str, str]:
    try:
        target = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadValidationError(f"unable to read Base target: {path}") from exc
    if not isinstance(target, dict):
        raise PayloadValidationError(f"Base target must be a JSON object: {path}")
    base_token = target.get("base_token")
    table_id = target.get("table_id")
    if not isinstance(base_token, str) or not base_token.strip():
        raise PayloadValidationError("Base target has no base_token")
    if not isinstance(table_id, str) or not table_id.strip():
        raise PayloadValidationError("Base target has no table_id")
    base_token = base_token.strip()
    url = target.get("url")
    if not isinstance(url, str) or not url.strip():
        url = f"https://feishu.cn/base/{base_token}"
    return {
        "base_token": base_token,
        "table_id": table_id.strip(),
        "url": url.strip(),
    }


def read_base_target(path: Path) -> tuple[str, str]:
    target = read_base_target_details(path)
    return target["base_token"], target["table_id"]


def _find_named_value(value: Any, names: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] not in (None, ""):
                return value[name]
        for child in value.values():
            found = _find_named_value(child, names)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_named_value(child, names)
            if found not in (None, ""):
                return found
    return None


def _find_base_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "base_url", "link"):
            candidate = value.get(key)
            if isinstance(candidate, str) and "/base/" in candidate:
                return candidate
        for child in value.values():
            found = _find_base_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_base_url(child)
            if found:
                return found
    return None


def _response_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    return data if isinstance(data, dict) else response


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _decode_base_create_response(
    response: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Read resource IDs only from their documented Base and table objects."""

    data = _response_data(response)
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    table = data.get("table") if isinstance(data.get("table"), dict) else {}
    base_token = _first_string(
        base.get("base_token"),
        base.get("app_token"),
        data.get("base_token"),
        data.get("app_token"),
    )
    table_id = _first_string(
        table.get("table_id"),
        table.get("id"),
        data.get("table_id"),
    )
    url = _first_string(
        base.get("url"),
        base.get("base_url"),
        base.get("link"),
        data.get("url"),
        data.get("base_url"),
        data.get("link"),
    )
    return base_token, table_id, url


def _redact_response(value: Any) -> Any:
    sensitive_keys = {
        "access_token",
        "refresh_token",
        "tenant_access_token",
        "user_access_token",
        "authorization",
        "cookie",
        "password",
        "secret",
        "api_key",
    }
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in sensitive_keys
            else _redact_response(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_response(child) for child in value]
    return value


def _create_response_snapshot_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.create-response.json")


def _table_list_snapshot_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.table-list-response.json")


def _recover_table_id_from_list(
    response: dict[str, Any], table_name: str
) -> str | None:
    data = _response_data(response)
    tables = data.get("tables")
    if not isinstance(tables, list):
        return None
    expected_name = table_name.strip()
    matches: list[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        name = _first_string(table.get("name"), table.get("table_name"))
        table_id = _first_string(table.get("table_id"), table.get("id"))
        if name == expected_name and table_id:
            matches.append(table_id)
    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) > 1:
        raise RuntimeError(
            f"table-list returned multiple tables named {table_name!r}; "
            "refusing to guess the new table ID"
        )
    return unique_matches[0] if unique_matches else None


def _decode_json_output(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"lark-cli returned no JSON: {stdout[-1000:]}")


def _find_lark_cli_executable() -> str | None:
    direct = shutil.which("lark-cli.exe")
    if direct:
        return direct
    command_wrapper = shutil.which("lark-cli.cmd")
    if command_wrapper:
        bundled = (
            Path(command_wrapper).parent
            / "node_modules"
            / "@larksuite"
            / "cli"
            / "bin"
            / "lark-cli.exe"
        )
        if bundled.is_file():
            return str(bundled)
        return command_wrapper
    return shutil.which("lark-cli")


class LarkCliClient:
    def __init__(
        self,
        identity: str = "user",
        executable: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.identity = identity
        self.executable = executable or _find_lark_cli_executable()
        if not self.executable:
            raise RuntimeError("lark-cli executable not found")
        self.runner = runner or subprocess.run

    def call(self, args: list[str]) -> dict[str, Any]:
        completed = self.runner(
            [self.executable, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise RuntimeError(f"lark-cli failed ({completed.returncode}): {output[-2000:]}")
        body = _decode_json_output(output)
        if body.get("ok") is False:
            raise RuntimeError(json.dumps(body, ensure_ascii=False))
        return body

    def batch_create(
        self, base_token: str, table_id: str, records: list[dict[str, Any]]
    ) -> list[str]:
        created_ids: list[str] = []
        for batch in chunks(records):
            with tempfile.TemporaryDirectory(dir=str(Path.cwd())) as temp_dir:
                payload_path = Path(temp_dir) / "batch-create.json"
                payload_path.write_text(
                    json.dumps({"create_records": batch}, ensure_ascii=False),
                    encoding="utf-8",
                )
                body = self.call(
                    [
                        "base",
                        "+record-batch-create",
                        "--base-token",
                        base_token,
                        "--table-id",
                        table_id,
                        "--json",
                        f"@{payload_path.relative_to(Path.cwd())}",
                        "--as",
                        self.identity,
                        "--format",
                        "json",
                    ]
                )
                ids = body.get("data", {}).get("record_id_list", [])
                if len(ids) != len(batch):
                    raise RuntimeError(
                        f"batch-create returned {len(ids)} IDs for {len(batch)} records"
                    )
                created_ids.extend(ids)
        return created_ids

    def create_base(
        self,
        name: str,
        table_name: str,
        fields: list[dict[str, str]],
        *,
        folder_token: str | None = None,
        time_zone: str = "Asia/Shanghai",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        args = [
            "base",
            "+base-create",
            "--name",
            name,
            "--table-name",
            table_name,
            "--fields",
            json.dumps(fields, ensure_ascii=False, separators=(",", ":")),
            "--time-zone",
            time_zone,
            "--as",
            self.identity,
            "--format",
            "json",
        ]
        if folder_token:
            args.extend(["--folder-token", folder_token])
        if dry_run:
            args.append("--dry-run")
        return self.call(args)

    def list_tables(self, base_token: str) -> dict[str, Any]:
        return self.call(
            [
                "base",
                "+table-list",
                "--base-token",
                base_token,
                "--limit",
                "100",
                "--as",
                self.identity,
                "--format",
                "json",
            ]
        )


def create_base_target(
    client: LarkCliClient,
    mapping_path: Path,
    *,
    name: str,
    table_name: str,
    output_path: Path,
    folder_token: str | None = None,
    time_zone: str = "Asia/Shanghai",
    dry_run: bool = False,
) -> dict[str, Any]:
    fields = base_fields_from_mapping(mapping_path)
    if output_path.is_file() and not dry_run:
        try:
            read_base_target_details(output_path)
            existing = _read_json_object(output_path, "Base target")
            existing["reused_existing_target"] = True
            return existing
        except (MappingValidationError, PayloadValidationError):
            pass

    response_snapshot_path = _create_response_snapshot_path(output_path)
    reused_create_response = response_snapshot_path.is_file() and not dry_run
    if reused_create_response:
        response = _read_json_object(
            response_snapshot_path, "saved base-create response"
        )
    else:
        response = client.create_base(
            name,
            table_name,
            fields,
            folder_token=folder_token,
            time_zone=time_zone,
            dry_run=dry_run,
        )
    if dry_run:
        result = {
            "dry_run": True,
            "name": name,
            "table_name": table_name,
            "fields": fields,
            "request": response,
        }
        write_json_file(result, output_path)
        return result


    # Persist the successful remote response before local contract decoding so
    # a rerun can recover without repeating the side-effecting create request.
    if not reused_create_response:
        write_json_file(_redact_response(response), response_snapshot_path)

    base_token, table_id, url = _decode_base_create_response(response)
    if not base_token:
        raise RuntimeError(
            "base-create returned no base_token; successful response saved at "
            f"{response_snapshot_path}"
        )

    recovered_table_id = False
    if not table_id:
        try:
            table_list_response = client.list_tables(base_token)
        except Exception as exc:
            raise RuntimeError(
                f"Base {base_token} was created but table ID recovery failed. "
                f"Rerun create-base with the same --output {output_path} to recover "
                "without creating another Base."
            ) from exc
        write_json_file(
            _redact_response(table_list_response),
            _table_list_snapshot_path(output_path),
        )
        table_id = _recover_table_id_from_list(table_list_response, table_name)
        if not table_id:
            raise RuntimeError(
                f"Base {base_token} was created, but no table named {table_name!r} "
                "was found. Rerun create-base with the same --output to retry "
                "recovery without creating another Base."
            )
        recovered_table_id = True

    result = {
        "dry_run": False,
        "base_token": base_token,
        "table_id": table_id,
        "url": url or f"https://feishu.cn/base/{base_token}",
        "name": name,
        "table_name": table_name,
        "fields": fields,
        "create_response_snapshot": str(response_snapshot_path.resolve()),
        "reused_create_response": reused_create_response,
        "recovered_table_id": recovered_table_id,
    }
    permission_grant = _find_named_value(response, ("permission_grant",))
    if permission_grant is not None:
        result["permission_grant"] = permission_grant
    write_json_file(result, output_path)
    return result


def create_payload(
    client: LarkCliClient,
    base_token: str,
    table_id: str,
    payload_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    records = read_payload(payload_path)
    result: dict[str, Any] = {
        "mode": "create_only",
        "incoming": len(records),
        "to_create": len(records),
        "batch_count": (len(records) + BATCH_SIZE - 1) // BATCH_SIZE,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    created_ids = client.batch_create(base_token, table_id, records)
    result["created_record_count"] = len(created_ids)
    return result


def start_create_payload(
    client: LarkCliClient,
    target_path: Path,
    payload_path: Path,
    remaining_output_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the first batch before making the Base preview URL available."""

    target = read_base_target_details(target_path)
    records = read_payload(payload_path)
    if not records:
        raise PayloadValidationError(
            "payload contains no records; refusing to expose an empty Base preview"
        )
    first_batch = records[:BATCH_SIZE]
    remaining = records[BATCH_SIZE:]
    result: dict[str, Any] = {
        "mode": "create_only",
        "phase": "first_batch_pending" if dry_run else "first_batch_written",
        "incoming": len(records),
        "first_batch_count": len(first_batch),
        "remaining_record_count": len(remaining),
        "batch_count": (len(records) + BATCH_SIZE - 1) // BATCH_SIZE,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    created_ids = client.batch_create(
        target["base_token"], target["table_id"], first_batch
    )
    continuation = {
        "version": 2,
        "mode": "create_only",
        "source": {"continued_from": str(payload_path.resolve())},
        "create_records": remaining,
    }
    write_prepared_payload(continuation, remaining_output_path)
    result.update(
        {
            "created_record_count": len(created_ids),
            "preview_url": target["url"],
            "continuation_payload": str(remaining_output_path.resolve()),
        }
    )
    return result


def _add_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-token")
    parser.add_argument("--table-id")
    parser.add_argument(
        "--target",
        type=Path,
        help="target JSON written by create-base; replaces --base-token and --table-id",
    )
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--as", dest="identity", default="user", choices=("user", "bot"))
    parser.add_argument("--dry-run", action="store_true")


def _add_start_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--remaining-output", required=True, type=Path)
    parser.add_argument("--as", dest="identity", default="user", choices=("user", "bot"))
    parser.add_argument("--dry-run", action="store_true")


def _add_create_base_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--table-name", default="数据")
    parser.add_argument("--folder-token")
    parser.add_argument("--time-zone", default="Asia/Shanghai")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as", dest="identity", default="user", choices=("user", "bot"))
    parser.add_argument("--dry-run", action="store_true")


def _add_prepare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="source_json", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)


def _add_auto_prepare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="source_json", required=True, type=Path)
    parser.add_argument("--table-index", type=int)
    parser.add_argument("--header-row", type=int, default=0)
    parser.add_argument("--data-start-row", type=int)
    parser.add_argument("--skip-empty-rows", action="store_true")
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--mapping-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)


def _create_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.target is not None:
        if args.base_token or args.table_id:
            raise PayloadValidationError(
                "use either --target or --base-token/--table-id, not both"
            )
        base_token, table_id = read_base_target(args.target)
    else:
        if not args.base_token or not args.table_id:
            raise PayloadValidationError(
                "create requires --target or both --base-token and --table-id"
            )
        base_token, table_id = args.base_token, args.table_id
    client = LarkCliClient(identity=args.identity)
    return create_payload(
        client,
        base_token,
        table_id,
        args.payload,
        args.dry_run,
    )


def _start_create_from_args(args: argparse.Namespace) -> dict[str, Any]:
    client = LarkCliClient(identity=args.identity)
    return start_create_payload(
        client,
        args.target,
        args.payload,
        args.remaining_output,
        args.dry_run,
    )


def _create_base_from_args(args: argparse.Namespace) -> dict[str, Any]:
    client = LarkCliClient(identity=args.identity)
    target = create_base_target(
        client,
        args.mapping,
        name=args.name,
        table_name=args.table_name,
        output_path=args.output,
        folder_token=args.folder_token,
        time_zone=args.time_zone,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return target
    return {
        "status": "base_created_waiting_for_first_batch",
        "target": str(args.output.resolve()),
        "field_count": len(target["fields"]),
    }


def _prepare_from_args(args: argparse.Namespace) -> dict[str, Any]:
    payload = prepare_payload(
        args.source_json,
        args.mapping,
        source_file=args.source_file,
    )
    write_prepared_payload(payload, args.output)
    return {
        "payload": str(args.output.resolve()),
        "record_count": len(payload["create_records"]),
        "mode": payload["mode"],
    }


def _auto_prepare_from_args(args: argparse.Namespace) -> dict[str, Any]:
    mapping = mapping_from_somark_json(
        args.source_json,
        table_index=args.table_index,
        header_row=args.header_row,
        data_start_row=args.data_start_row,
        skip_empty_rows=args.skip_empty_rows,
    )
    write_json_file(mapping, args.mapping_output)
    payload = prepare_payload(
        args.source_json,
        args.mapping_output,
        source_file=args.source_file,
    )
    write_prepared_payload(payload, args.output)
    return {
        "mapping": str(args.mapping_output.resolve()),
        "payload": str(args.output.resolve()),
        "field_count": len(mapping["fields"]),
        "record_count": len(payload["create_records"]),
        "mode": payload["mode"],
        "warnings": mapping.get("warnings", []),
        "requires_confirmation": bool(mapping.get("warnings")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare SoMark records and append them to Feishu Bitable"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_parser = commands.add_parser(
        "prepare", help="map SoMark JSON table rows to a reviewable record payload"
    )
    _add_prepare_arguments(prepare_parser)

    auto_prepare_parser = commands.add_parser(
        "auto-prepare",
        help="derive headers from SoMark JSON and prepare records without OCR",
    )
    _add_auto_prepare_arguments(auto_prepare_parser)

    create_base_parser = commands.add_parser(
        "create-base", help="create an empty Base with fields derived from the mapping"
    )
    _add_create_base_arguments(create_base_parser)

    create_parser = commands.add_parser(
        "create", help="append every prepared record to an existing Bitable table"
    )
    _add_create_arguments(create_parser)

    start_create_parser = commands.add_parser(
        "start-create",
        help="write the first batch, then return the non-empty Base preview URL",
    )
    _add_start_create_arguments(start_create_parser)

    run_parser = commands.add_parser(
        "run", help="prepare a payload, save it, then append all records to Bitable"
    )
    run_parser.add_argument("--json", dest="source_json", required=True, type=Path)
    run_parser.add_argument("--mapping", required=True, type=Path)
    run_parser.add_argument("--source-file", type=Path)
    run_parser.add_argument("--payload-output", required=True, type=Path)
    run_parser.add_argument("--base-token", required=True)
    run_parser.add_argument("--table-id", required=True)
    run_parser.add_argument("--as", dest="identity", default="user", choices=("user", "bot"))
    run_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.command == "prepare":
        result = _prepare_from_args(args)
    elif args.command == "auto-prepare":
        result = _auto_prepare_from_args(args)
    elif args.command == "create-base":
        result = _create_base_from_args(args)
    elif args.command == "start-create":
        result = _start_create_from_args(args)
    elif args.command == "create":
        result = _create_from_args(args)
    else:
        payload = prepare_payload(
            args.source_json,
            args.mapping,
            source_file=args.source_file,
        )
        write_prepared_payload(payload, args.payload_output)
        create_args = argparse.Namespace(
            base_token=args.base_token,
            table_id=args.table_id,
            target=None,
            payload=args.payload_output,
            identity=args.identity,
            dry_run=args.dry_run,
        )
        result = {
            "prepare": {
                "payload": str(args.payload_output.resolve()),
                "record_count": len(payload["create_records"]),
                "mode": payload["mode"],
            },
            "create": _create_from_args(create_args),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
