"""DWS-only execution and readback verification for DingTalk AI Tables."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from .artifacts import RouteName, RouteResult, RouteTarget, SourceArtifacts
from .dws_runner import DwsRunResult, DwsRunner
from .errors import ErrorKind, StructuredError, redact_sensitive
from .manifest import ManifestStage, new_manifest, set_stage, write_manifest_atomic
from .aitable_models import AitableFieldPlan, AitablePlan
from .aitable_planner import (
    DWS_CONTRACT_VERSION,
    MANIFEST_FILENAME,
    _write_json_atomic,
    normalize_date_value,
    plan_aitable_route,
)


FIELD_ID_MAP_FILENAME = "aitable_field_ids.json"
READBACK_FILENAME = "aitable_readback.json"
DIAGNOSTIC_FILENAME = "aitable_diagnostic.json"
RECORD_DRY_RUN_FILENAME = "aitable_records_dry_run.json"
_HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_BLANK_PRIMARY_SENTINEL = " "


def _accept_explicit_empty_error_success(result: DwsRunResult) -> DwsRunResult:
    """Handle the DWS 1.0.57 success envelope without weakening failure checks."""

    payload = result.stdout
    if (
        result.exit_code == 0
        and result.error is not None
        and isinstance(payload, Mapping)
        and payload.get("success") is True
        and payload.get("status") == "success"
        and isinstance(payload.get("error"), Mapping)
        and not payload["error"]
    ):
        return DwsRunResult(
            command=result.command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            error=None,
        )
    return result


def _safe_run_entry(
    operation: str,
    attempts: Sequence[DwsRunResult],
    **validation: Any,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "attempt_count": len(attempts),
        "attempts": [attempt.to_safe_dict() for attempt in attempts],
        "validation": {
            "command_succeeded": bool(attempts and attempts[-1].command_succeeded),
            **redact_sensitive(validation),
        },
    }


def _run_with_retry_once(
    runner: DwsRunner,
    arguments: Sequence[str],
    *,
    profile: str | None,
    timeout_seconds: float = 120.0,
    dry_run: bool = False,
) -> tuple[DwsRunResult, list[DwsRunResult]]:
    first = _accept_explicit_empty_error_success(
        runner.run_json(
            list(arguments),
            profile=profile,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
        )
    )
    attempts = [first]
    if first.error is not None and first.error.may_retry_once:
        attempts.append(
            _accept_explicit_empty_error_success(
                runner.run_json(
                    list(arguments),
                    profile=profile,
                    timeout_seconds=timeout_seconds,
                    dry_run=dry_run,
                )
            )
        )
    return attempts[-1], attempts


def _payload_data(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return None
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_base_identity(payload: Any) -> tuple[str | None, str | None]:
    data = _payload_data(payload)
    if data is None:
        return None, None
    base = data.get("base") if isinstance(data.get("base"), Mapping) else {}
    return (
        _first_string(data.get("baseId"), data.get("base_id"), base.get("baseId"), base.get("id")),
        _first_string(data.get("url"), data.get("link"), base.get("url"), base.get("link")),
    )


def _extract_tables(payload: Any) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    if data is None:
        return []
    base = data.get("base") if isinstance(data.get("base"), Mapping) else {}
    raw = data.get("tables")
    if not isinstance(raw, list):
        raw = base.get("tables")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _extract_table_identity(payload: Any) -> tuple[str | None, str | None]:
    data = _payload_data(payload)
    if data is None:
        return None, None
    table = data.get("table") if isinstance(data.get("table"), Mapping) else {}
    return (
        _first_string(data.get("tableId"), data.get("table_id"), table.get("tableId"), table.get("id")),
        _first_string(data.get("name"), data.get("tableName"), table.get("name"), table.get("tableName")),
    )


def _extract_fields(payload: Any) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    if data is None:
        return []
    raw: Any = None
    for key in ("fields", "items", "fieldList", "field_list"):
        if isinstance(data.get(key), list):
            raw = data[key]
            break
    if raw is None and isinstance(data.get("table"), Mapping):
        table = data["table"]
        raw = table.get("fields")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _field_name(field: Mapping[str, Any]) -> str | None:
    return _first_string(field.get("name"), field.get("fieldName"), field.get("field_name"))


def _field_id(field: Mapping[str, Any]) -> str | None:
    return _first_string(field.get("fieldId"), field.get("field_id"), field.get("id"))


def _field_type(field: Mapping[str, Any]) -> str | None:
    return _first_string(field.get("type"), field.get("fieldType"), field.get("field_type"))


def _canonical_type(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    aliases = {
        "primarydoc": "text",
        "string": "text",
        "select": "singleselect",
        "datetime": "date",
    }
    return aliases.get(normalized, normalized)


def _option_names(field: Mapping[str, Any]) -> list[str]:
    config = field.get("config")
    raw = config.get("options") if isinstance(config, Mapping) else field.get("options")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str):
            names.append(name)
    return names


def _dry_run_accepts_fields(payload: Any, expected: list[dict[str, Any]]) -> bool:
    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return False
    params: Any = payload.get("arguments")
    if not isinstance(params, Mapping):
        invocation = payload.get("invocation")
        data = payload.get("data")
        if not isinstance(invocation, Mapping) and isinstance(data, Mapping):
            invocation = data.get("invocation")
        if isinstance(invocation, Mapping):
            params = invocation.get("params")
    if not isinstance(params, Mapping) or "fields" not in params:
        return False
    retained = params["fields"]
    if isinstance(retained, str):
        try:
            retained = json.loads(retained)
        except json.JSONDecodeError:
            return False
    return retained == expected


def _extract_record_ids(payload: Any) -> list[str]:
    data = _payload_data(payload)
    if data is None:
        return []
    raw: Any = None
    for key in ("recordIds", "record_ids", "newRecordIds", "new_record_ids"):
        if isinstance(data.get(key), list):
            raw = data[key]
            break
    if raw is None and isinstance(data.get("records"), list):
        raw = [
            item.get("recordId") or item.get("record_id")
            for item in data["records"]
            if isinstance(item, Mapping)
        ]
    return [str(item) for item in raw or [] if isinstance(item, str) and item]


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    if data is None:
        return []
    raw = data.get("records")
    if not isinstance(raw, list):
        raw = data.get("items")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _record_id(record: Mapping[str, Any]) -> str | None:
    return _first_string(record.get("recordId"), record.get("record_id"), record.get("id"))


def _record_cells(record: Mapping[str, Any]) -> Mapping[str, Any]:
    cells = record.get("cells")
    return cells if isinstance(cells, Mapping) else {}


def _direct_urls(base_id: str, table_id: str) -> tuple[str, str]:
    root = f"https://docs.dingtalk.com/i/nodes/{quote(base_id, safe='')}"
    direct = root + "?iframeQuery=sheetId%3D" + quote(table_id, safe="")
    return root, direct


def _field_readback_summary(
    fields: Sequence[Mapping[str, Any]],
    plan_fields: Sequence[AitableFieldPlan],
) -> tuple[dict[str, str], dict[str, Any]]:
    expected_names = [field.source_name for field in plan_fields]
    matches: dict[str, list[Mapping[str, Any]]] = {name: [] for name in expected_names}
    for item in fields:
        name = _field_name(item)
        if name in matches:
            matches[name].append(item)

    field_ids: dict[str, str] = {}
    missing_or_duplicate: list[str] = []
    type_checks: dict[str, bool] = {}
    option_checks: dict[str, bool] = {}
    summaries: list[dict[str, Any]] = []
    for plan_field in plan_fields:
        candidates = matches[plan_field.source_name]
        if len(candidates) != 1:
            missing_or_duplicate.append(plan_field.source_name)
            continue
        item = candidates[0]
        identifier = _field_id(item)
        actual_type = _canonical_type(_field_type(item))
        expected_type = _canonical_type(plan_field.target_type)
        type_checks[plan_field.source_name] = bool(identifier) and actual_type == expected_type
        expected_options = [
            str(option.get("name"))
            for option in plan_field.config.get("options", [])
            if isinstance(option, Mapping) and option.get("name") is not None
        ]
        option_checks[plan_field.source_name] = (
            not expected_options or _option_names(item) == expected_options
        )
        if identifier:
            field_ids[plan_field.source_name] = identifier
        summaries.append(
            {
                "source_name": plan_field.source_name,
                "fieldId": identifier,
                "expected_type": plan_field.target_type,
                "actual_type": _field_type(item),
                "options": _option_names(item),
            }
        )

    business_order = [
        name for name in (_field_name(item) for item in fields) if name in set(expected_names)
    ]
    first_business_index = next(
        (index for index, item in enumerate(fields) if _field_name(item) in set(expected_names)),
        None,
    )
    primary_ok = (
        first_business_index == 0
        and bool(fields)
        and _field_name(fields[0]) == expected_names[0]
        and _canonical_type(_field_type(fields[0])) == "text"
    )
    checks = {
        "field_names_unique_and_complete": not missing_or_duplicate and len(field_ids) == len(plan_fields),
        "field_order_preserved": business_order == expected_names,
        "field_types_match": bool(type_checks) and all(type_checks.values()),
        "select_option_order_preserved": bool(option_checks) and all(option_checks.values()),
        "primary_field_is_source_first_text": primary_ok,
    }
    return field_ids, {
        "fields": summaries,
        "business_field_order": business_order,
        "missing_or_duplicate": missing_or_duplicate,
        "type_checks": type_checks,
        "option_checks": option_checks,
        "checks": checks,
    }


def _semantic_value(value: Any, field_type: str, timezone: str) -> Any:
    canonical = _canonical_type(field_type)
    if canonical == "singleselect":
        if isinstance(value, Mapping):
            return value.get("name")
        return str(value) if value is not None else None
    if canonical in {"number", "currency", "progress", "rating"}:
        try:
            return Decimal(str(value)) if value is not None else None
        except InvalidOperation:
            return value
    if canonical == "date":
        try:
            return normalize_date_value(value, timezone) if value is not None else None
        except ValueError:
            return value
    return value


def _compare_cell(
    expected_record: Mapping[str, Any],
    actual_cells: Mapping[str, Any],
    field: AitableFieldPlan,
    field_id: str,
    timezone: str,
) -> tuple[bool, dict[str, Any]]:
    expected_present = field.source_name in expected_record
    actual_present = field_id in actual_cells
    if not expected_present:
        actual = actual_cells.get(field_id)
        passed = not actual_present or actual in (None, "", _BLANK_PRIMARY_SENTINEL)
        return passed, {
            "field": field.source_name,
            "expected_state": "missing",
            "actual_state": "blank" if actual_present and actual in (None, "", _BLANK_PRIMARY_SENTINEL) else (
                "present" if actual_present else "missing"
            ),
            "passed": passed,
        }
    expected = expected_record[field.source_name]
    actual = actual_cells.get(field_id)
    passed = actual_present and (
        _semantic_value(expected, field.target_type, timezone)
        == _semantic_value(actual, field.target_type, timezone)
    )
    return passed, {
        "field": field.source_name,
        "expected_state": "null" if expected is None else "empty_string" if expected == "" else "value",
        "actual_state": "missing" if not actual_present else "null" if actual is None else "empty_string" if actual == "" else "value",
        "expected": expected,
        "actual": actual,
        "passed": passed,
    }


def _verification_summary(
    plan: AitablePlan,
    field_ids: Mapping[str, str],
    created_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    *,
    timezone: str,
) -> dict[str, Any]:
    actual_ids = [_record_id(record) for record in records]
    actual_ids_clean = [item for item in actual_ids if item]
    actual_by_id = {
        identifier: record
        for record in records
        if (identifier := _record_id(record)) is not None
    }
    primary = plan.field_plans[0]
    primary_id = field_ids.get(primary.source_name)
    primary_order = [
        None
        if (value := (_record_cells(record).get(primary_id) if primary_id else None))
        in (None, "", _BLANK_PRIMARY_SENTINEL)
        else value
        for record in records
    ]
    expected_primary_order = [
        None
        if (value := record.get(primary.source_name)) in (None, "", _BLANK_PRIMARY_SENTINEL)
        else value
        for record in plan.normalized_records
    ]

    detail_checks: list[dict[str, Any]] = []
    for index in dict.fromkeys((0, len(plan.normalized_records) - 1)):
        if index < 0 or index >= len(created_ids):
            continue
        actual_record = actual_by_id.get(created_ids[index], {})
        cells = _record_cells(actual_record)
        for field in plan.field_plans:
            if not field.record_writable or field.source_name not in field_ids:
                continue
            _, detail = _compare_cell(
                plan.normalized_records[index],
                cells,
                field,
                field_ids[field.source_name],
                timezone,
            )
            detail.update({"scope": "first_or_last", "source_index": index})
            detail_checks.append(detail)

    representatives: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    for field in plan.field_plans:
        if not field.record_writable or field.source_name not in field_ids:
            continue
        canonical = _canonical_type(field.target_type)
        if canonical in seen_types:
            continue
        index = next(
            (
                current
                for current, record in enumerate(plan.normalized_records)
                if field.source_name in record and record[field.source_name] not in (None, "")
            ),
            None,
        )
        if index is None or index >= len(created_ids):
            continue
        seen_types.add(canonical)
        actual_record = actual_by_id.get(created_ids[index], {})
        passed, detail = _compare_cell(
            plan.normalized_records[index],
            _record_cells(actual_record),
            field,
            field_ids[field.source_name],
            timezone,
        )
        detail.update({"scope": "representative", "source_index": index, "type": field.target_type})
        representatives.append(detail)

    checks = {
        "record_count_matches": len(records) == len(created_ids) == len(plan.normalized_records),
        "record_id_set_matches": len(actual_ids_clean) == len(created_ids) and set(actual_ids_clean) == set(created_ids),
        "primary_field_order_preserved": primary_order == expected_primary_order,
        "first_and_last_records_match": bool(detail_checks) and all(item["passed"] for item in detail_checks),
        "representative_types_match": bool(representatives) and all(item["passed"] for item in representatives),
    }
    return {
        "tableId": None,
        "recordIds": list(created_ids),
        "actualRecordIds": actual_ids_clean,
        "expectedPrimaryOrder": expected_primary_order,
        "actualPrimaryOrder": primary_order,
        "first_last_checks": detail_checks,
        "representative_checks": representatives,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def _degradation_strings(items: Iterable[Any]) -> list[str]:
    rendered: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            rendered.append(f"{item.get('category', 'degradation')}: {item.get('message', item.get('code', ''))}")
        else:
            rendered.append(str(item))
    return rendered


def _set_result(
    result: RouteResult,
    manifest: Mapping[str, Any],
    evidence_files: Sequence[str],
) -> None:
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


def _plan_failure_result(
    source: SourceArtifacts,
    target: RouteTarget,
    exc: Exception,
) -> RouteResult:
    error = StructuredError(
        ErrorKind.BUSINESS_VALIDATION,
        str(exc),
        details={"phase": "plan_aitable_route"},
    )
    result = RouteResult(route=RouteName.AITABLE, stage=ManifestStage.FAILED.value)
    result.error = error.to_safe_dict()
    try:
        evidence_dir = Path(target.evidence_dir).expanduser().resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        source_hash = source.source_hash if _HEX_64_RE.fullmatch(source.source_hash) else "0" * 64
        manifest = new_manifest(
            route=RouteName.AITABLE.value,
            source=source.source_path or source.json_path,
            source_hash=source_hash.casefold(),
            somark_artifacts={"json": source.json_path, "markdown": source.markdown_path},
            dws_cli_version=DWS_CONTRACT_VERSION,
            target={"baseId": None, "tableId": None, "title": target.title, "direct_url": None},
        )
        manifest["ledger"].append(
            {"operation": "plan_aitable_route", "validation": {"success": False}}
        )
        set_stage(manifest, ManifestStage.FAILED, error=error.to_safe_dict())
        path = write_manifest_atomic(evidence_dir / MANIFEST_FILENAME, manifest)
        _set_result(result, manifest, [str(path)])
    except Exception:
        pass
    return result


def _finish_with_error(
    result: RouteResult,
    manifest: dict[str, Any],
    plan: AitablePlan,
    error: StructuredError,
    *,
    partial: bool,
    started: float,
    evidence_files: Sequence[str],
) -> RouteResult:
    set_stage(
        manifest,
        ManifestStage.PARTIAL if partial else ManifestStage.FAILED,
        error=error.to_safe_dict(),
    )
    manifest["timings"]["total_seconds"] = monotonic() - started
    write_manifest_atomic(plan.manifest_path, manifest)
    _set_result(result, manifest, evidence_files)
    return result


def _build_record_payloads(
    plan: AitablePlan,
    field_ids: Mapping[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Build DWS record payloads while preserving visually blank source rows."""

    field_by_name = {field.source_name: field for field in plan.field_plans}
    primary_field = next(
        (
            field
            for field in plan.field_plans
            if field.primary and field.record_writable and field.source_name in field_ids
        ),
        None,
    )
    if primary_field is None:
        primary_field = next(
            (
                field
                for field in plan.field_plans
                if field.record_writable and field.source_name in field_ids
            ),
            None,
        )
    primary_field_id = field_ids.get(primary_field.source_name) if primary_field else None

    payloads: list[dict[str, Any]] = []
    blank_transport_fills = 0
    for source_record in plan.normalized_records:
        cells: dict[str, Any] = {}
        for name, value in source_record.items():
            field = field_by_name[name]
            if field.record_writable:
                cells[field_ids[name]] = value
        if not cells:
            if primary_field_id is None:
                raise ValueError("cannot transport a blank AI Table record without a writable primary field")
            cells[primary_field_id] = _BLANK_PRIMARY_SENTINEL
            blank_transport_fills += 1
        payloads.append({"cells": cells})
    return payloads, blank_transport_fills


def run_aitable_route(
    source: SourceArtifacts,
    target: RouteTarget,
    *,
    runner: DwsRunner | None = None,
    execute: bool = False,
    timezone: str = "Asia/Shanghai",
    fast_mode: bool = True,
    plan_callback: Callable[[Mapping[str, Any]], None] | None = None,
    preview_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> RouteResult:
    """Plan locally by default; create one new Base/table and verify when enabled."""

    started = monotonic()
    try:
        plan = plan_aitable_route(source, target, timezone=timezone)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _plan_failure_result(source, target, exc)

    result = RouteResult(route=RouteName.AITABLE)
    evidence_files = [
        plan.field_plan_path,
        plan.record_plan_path,
        plan.degradation_plan_path,
        plan.manifest_path,
    ]
    manifest = json.loads(Path(plan.manifest_path).read_text(encoding="utf-8"))
    manifest["timings"]["planning_seconds"] = monotonic() - started
    if plan_callback is not None:
        try:
            plan_callback(
                {
                    "event": "plan_completed",
                    "route": RouteName.AITABLE.value,
                    "planning_seconds": manifest["timings"]["planning_seconds"],
                    "field_count": len(plan.field_plans),
                    "record_count": len(plan.normalized_records),
                    "manifest": plan.manifest_path,
                }
            )
        except Exception as exc:
            manifest["warnings"].append(
                f"Plan callback failed after local planning: {type(exc).__name__}"
            )
    if not execute:
        write_manifest_atomic(plan.manifest_path, manifest)
        _set_result(result, manifest, evidence_files)
        return result

    if not plan.route_eligible:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "AI Table conversion was not executed; the source should use the sheet route",
            details={"recommended_route": plan.recommended_route, "reasons": list(plan.routing_reasons)},
        )
        return _finish_with_error(
            result,
            manifest,
            plan,
            error,
            partial=True,
            started=started,
            evidence_files=evidence_files,
        )

    active_runner = runner or DwsRunner(expected_version=DWS_CONTRACT_VERSION)
    version, version_error = active_runner.read_version()
    manifest["dws_cli_version"] = version or DWS_CONTRACT_VERSION
    manifest["ledger"].append(
        {
            "operation": "dws_version",
            "command": ["dws", "--version"],
            "validation": {
                "expected": DWS_CONTRACT_VERSION,
                "actual": version,
                "matched": version_error is None,
            },
            "error": version_error.to_safe_dict() if version_error else None,
        }
    )
    if version_error is not None:
        return _finish_with_error(
            result,
            manifest,
            plan,
            version_error,
            partial=False,
            started=started,
            evidence_files=evidence_files,
        )

    set_stage(manifest, ManifestStage.RUNNING)
    manifest["ledger"].append({"operation": "stage", "stage": ManifestStage.RUNNING.value})
    write_manifest_atomic(plan.manifest_path, manifest)

    phase_started = monotonic()
    base_create, attempts = _run_with_retry_once(
        active_runner,
        ["aitable", "base", "create", "--name", plan.title],
        profile=target.profile,
        timeout_seconds=120.0,
    )
    base_id, returned_root = _extract_base_identity(base_create.stdout)
    manifest["ledger"].append(
        _safe_run_entry("aitable base create", attempts, baseId_present=bool(base_id))
    )
    if not base_create.command_succeeded or not base_id:
        error = base_create.error or StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "DWS base create succeeded without a documented baseId",
        )
        return _finish_with_error(
            result, manifest, plan, error, partial=False, started=started, evidence_files=evidence_files
        )
    manifest["timings"]["base_create_seconds"] = monotonic() - phase_started
    fixed_root = _direct_urls(base_id, "placeholder")[0]
    manifest["target"].update(
        {"baseId": base_id, "root_url": fixed_root, "returned_root_url": returned_root}
    )
    write_manifest_atomic(plan.manifest_path, manifest)

    if fast_mode:
        manifest["ledger"].append(
            {
                "operation": "aitable base get skipped",
                "mode": "fast",
                "validation": {
                    "pinned_dws_version": DWS_CONTRACT_VERSION,
                    "create_only": True,
                    "default_table_delete_requested": False,
                },
            }
        )
        manifest["readback"]["base_directory"] = {
            "baseId": base_id,
            "performed": False,
            "default_table_preserved": True,
            "preservation_basis": "create-only fast path never issues a table delete",
        }
        manifest["timings"]["directory_readback_seconds"] = 0.0
    else:
        phase_started = monotonic()
        base_get, attempts = _run_with_retry_once(
            active_runner,
            ["aitable", "base", "get", "--base-id", base_id],
            profile=target.profile,
        )
        default_tables = _extract_tables(base_get.stdout)
        manifest["ledger"].append(
            _safe_run_entry(
                "aitable base get",
                attempts,
                table_count=len(default_tables),
                default_table_preserved=True,
            )
        )
        if not base_get.command_succeeded or not default_tables:
            error = base_get.error or StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "Base directory readback did not expose the default table",
            )
            return _finish_with_error(
                result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
            )
        manifest["readback"]["base_directory"] = {
            "baseId": base_id,
            "tables": [
                {
                    "tableId": _first_string(item.get("tableId"), item.get("table_id"), item.get("id")),
                    "name": _first_string(item.get("name"), item.get("tableName")),
                }
                for item in default_tables
            ],
            "performed": True,
            "default_table_preserved": True,
        }
        manifest["timings"]["directory_readback_seconds"] = monotonic() - phase_started

    field_payload = plan.field_payload
    fields_json = json.dumps(field_payload, ensure_ascii=False, separators=(",", ":"))
    batch_table_args = [
        "aitable",
        "table",
        "create",
        "--base-id",
        base_id,
        "--name",
        plan.table_name,
        "--fields",
        fields_json,
    ]
    if fast_mode:
        batch_fields_accepted = True
        manifest["ledger"].append(
            {
                "operation": "aitable table create fields dry-run skipped",
                "mode": "fast",
                "validation": {
                    "pinned_dws_version": DWS_CONTRACT_VERSION,
                    "offline_contract_required": True,
                    "exact_field_count": len(field_payload),
                },
            }
        )
    else:
        dry_run, dry_attempts = _run_with_retry_once(
            active_runner,
            batch_table_args,
            profile=target.profile,
            dry_run=True,
        )
        batch_fields_accepted = dry_run.command_succeeded and _dry_run_accepts_fields(
            dry_run.stdout, field_payload
        )
        manifest["ledger"].append(
            _safe_run_entry(
                "aitable table create fields dry-run",
                dry_attempts,
                fields_retained=batch_fields_accepted,
                exact_field_count=len(field_payload),
            )
        )
    if not batch_fields_accepted:
        manifest["warnings"].append(
            "table create --fields dry-run did not retain the exact payload; conservative single-field path used"
        )
        manifest["degradations"].append(
            {
                "category": "contract_drift",
                "code": "table_create_fields_not_accepted",
                "message": "Schema/Help fields batch path was rejected or not retained; the platform default primary field is preserved",
            }
        )

    phase_started = monotonic()
    table_args = batch_table_args if batch_fields_accepted else [
        "aitable", "table", "create", "--base-id", base_id, "--name", plan.table_name
    ]
    table_create, attempts = _run_with_retry_once(
        active_runner,
        table_args,
        profile=target.profile,
        timeout_seconds=120.0,
    )
    table_id, returned_table_name = _extract_table_identity(table_create.stdout)
    manifest["ledger"].append(
        _safe_run_entry(
            "aitable table create",
            attempts,
            tableId_present=bool(table_id),
            batch_fields=batch_fields_accepted,
        )
    )
    if (not table_create.command_succeeded or not table_id) and fast_mode:
        diagnostic, diagnostic_attempts = _run_with_retry_once(
            active_runner,
            batch_table_args,
            profile=target.profile,
            dry_run=True,
        )
        manifest["ledger"].append(
            _safe_run_entry(
                "aitable table create controlled diagnostic",
                diagnostic_attempts,
                fields_retained=(
                    diagnostic.command_succeeded
                    and _dry_run_accepts_fields(diagnostic.stdout, field_payload)
                ),
            )
        )
    if not table_create.command_succeeded or not table_id:
        error = table_create.error or StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "DWS table create succeeded without a documented tableId",
        )
        return _finish_with_error(
            result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
        )
    root_url, direct_url = _direct_urls(base_id, table_id)
    manifest["target"].update(
        {
            "tableId": table_id,
            "table_name": returned_table_name or plan.table_name,
            "root_url": root_url,
            "direct_url": direct_url,
        }
    )
    manifest["timings"]["business_table_seconds"] = monotonic() - phase_started
    manifest["timings"]["link_seconds"] = monotonic() - started
    write_manifest_atomic(plan.manifest_path, manifest)

    field_batch_attempts: list[dict[str, Any]] = []
    if not batch_fields_accepted:
        for field in plan.field_plans:
            args = [
                "aitable",
                "field",
                "create",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--name",
                field.source_name,
                "--type",
                field.target_type,
            ]
            if field.config:
                args.extend(
                    ["--config", json.dumps(dict(field.config), ensure_ascii=False, separators=(",", ":"))]
                )
            created, attempts = _run_with_retry_once(
                active_runner,
                args,
                profile=target.profile,
            )
            field_batch_attempts.append(
                {"field": field.source_name, "attempts": [item.to_safe_dict() for item in attempts]}
            )
            if not created.command_succeeded:
                manifest["ledger"].append(
                    {
                        "operation": "aitable field create conservative batch",
                        "field_count": len(field_batch_attempts),
                        "items": field_batch_attempts,
                        "validation": {"success": False},
                    }
                )
                error = created.error or StructuredError(
                    ErrorKind.PROCESS_FAILURE,
                    f"DWS field create failed for {field.source_name}",
                )
                return _finish_with_error(
                    result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
                )
        manifest["ledger"].append(
            {
                "operation": "aitable field create conservative batch",
                "field_count": len(field_batch_attempts),
                "items": field_batch_attempts,
                "validation": {"success": True},
            }
        )
    else:
        manifest["ledger"].append(
            {
                "operation": "aitable field creation batch",
                "field_count": len(plan.field_plans),
                "via": "aitable table create --fields",
                "validation": {"transport_success": True},
            }
        )

    phase_started = monotonic()
    field_get, attempts = _run_with_retry_once(
        active_runner,
        [
            "aitable",
            "field",
            "get",
            "--base-id",
            base_id,
            "--table-id",
            table_id,
        ],
        profile=target.profile,
    )
    fields = _extract_fields(field_get.stdout)
    field_ids, field_summary = _field_readback_summary(fields, plan.field_plans)
    manifest["ledger"].append(
        _safe_run_entry(
            "aitable field get aggregate",
            attempts,
            field_count=len(fields),
            checks=field_summary["checks"],
        )
    )
    field_id_path = _write_json_atomic(
        Path(plan.evidence_dir) / FIELD_ID_MAP_FILENAME,
        {
            "schema_version": 1,
            "baseId": base_id,
            "tableId": table_id,
            "source_field_order": [field.source_name for field in plan.field_plans],
            "field_ids": field_ids,
            "readback": field_summary,
        },
    )
    evidence_files.append(str(field_id_path))
    manifest["readback"]["fields"] = field_summary
    manifest["statistics"]["actual_field_count"] = len(field_ids)
    manifest["statistics"]["field_batch_count"] = 1 if batch_fields_accepted else len(plan.field_plans)
    manifest["timings"]["field_readback_seconds"] = monotonic() - phase_started
    if not field_get.command_succeeded or not all(field_summary["checks"].values()):
        error = field_get.error or StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "field readback did not preserve field IDs, type/order, select options, and source-first text primary field",
            details={"checks": field_summary["checks"]},
        )
        return _finish_with_error(
            result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
        )

    record_payloads, blank_record_transport_fill_count = _build_record_payloads(plan, field_ids)
    manifest["statistics"]["blank_record_transport_fill_count"] = blank_record_transport_fill_count
    if blank_record_transport_fill_count:
        manifest["degradations"].append(
            {
                "category": "record_transport",
                "status": "blank_primary_transport_fill",
                "message": "blank source records were transported with a single-space primary-field sentinel because DWS rejects empty cells objects and strips empty strings; readback treats the sentinel as blank",
                "count": blank_record_transport_fill_count,
            }
        )

    if fast_mode:
        manifest["ledger"].append(
            {
                "operation": "aitable record create first-payload dry-run skipped",
                "mode": "fast",
                "validation": {
                    "pinned_dws_version": DWS_CONTRACT_VERSION,
                    "local_payload_validated": True,
                    "record_count": len(record_payloads),
                },
            }
        )
    else:
        dry_run_path = _write_json_atomic(
            Path(plan.evidence_dir) / RECORD_DRY_RUN_FILENAME,
            [record_payloads[0]],
        )
        evidence_files.append(str(dry_run_path))
        record_dry_run, attempts = _run_with_retry_once(
            active_runner,
            [
                "aitable",
                "record",
                "create",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--records-file",
                str(dry_run_path),
            ],
            profile=target.profile,
            dry_run=True,
        )
        manifest["ledger"].append(
            _safe_run_entry(
                "aitable record create first-payload dry-run",
                attempts,
                record_count=1,
                records_file=True,
            )
        )
        if not record_dry_run.command_succeeded:
            error = record_dry_run.error or StructuredError(
                ErrorKind.INVALID_ARGUMENT,
                "record payload dry-run failed; no records were written",
            )
            return _finish_with_error(
                result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
            )

    phase_started = monotonic()
    created_ids: list[str] = []
    record_batch_entries: list[dict[str, Any]] = []
    batch_size = 30 if fast_mode else 1
    for start in range(0, len(record_payloads), batch_size):
        batch = record_payloads[start : start + batch_size]
        batch_number = start // batch_size + 1
        batch_path = _write_json_atomic(
            Path(plan.evidence_dir) / f"aitable_records_batch_{batch_number:03d}.json",
            batch,
        )
        evidence_files.append(str(batch_path))
        created, attempts = _run_with_retry_once(
            active_runner,
            [
                "aitable",
                "record",
                "create",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--records-file",
                str(batch_path),
            ],
            profile=target.profile,
            timeout_seconds=180.0,
        )
        ids = _extract_record_ids(created.stdout)
        record_batch_entries.append(
            {
                "batch": batch_number,
                "record_count": len(batch),
                "records_file": str(batch_path),
                "record_id_count": len(ids),
                "attempts": [item.to_safe_dict() for item in attempts],
                "validation": {
                    "command_succeeded": created.command_succeeded,
                    "record_ids_match_batch": len(ids) == len(batch),
                },
            }
        )
        if not created.command_succeeded or len(ids) != len(batch):
            manifest["ledger"].append(
                {
                    "operation": "aitable record create batches",
                    "batches": record_batch_entries,
                    "validation": {"success": False},
                }
            )
            error = created.error or StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "record create returned an inconsistent recordId count",
                details={"expected": len(batch), "actual": len(ids), "batch": batch_number},
            )
            return _finish_with_error(
                result,
                manifest,
                plan,
                error,
                partial=bool(created_ids or ids),
                started=started,
                evidence_files=evidence_files,
            )
        created_ids.extend(ids)
        if batch_number == 1:
            manifest["statistics"]["written_record_count"] = len(created_ids)
            manifest["readback"]["preview_checkpoint"] = {
                "batch": batch_number,
                "recordIds": list(created_ids),
                "record_id_count_matches_batch": len(ids) == len(batch),
            }
            set_stage(manifest, ManifestStage.WRITTEN)
            write_manifest_atomic(plan.manifest_path, manifest)
            if preview_callback is not None:
                try:
                    preview_callback(
                        {
                            "event": "aitable_preview_ready",
                            "stage": ManifestStage.WRITTEN.value,
                            "baseId": base_id,
                            "tableId": table_id,
                            "recordIds": list(created_ids),
                            "direct_url": direct_url,
                            "postprocess_pending": True,
                        }
                    )
                except Exception as exc:
                    manifest["warnings"].append(
                        f"Preview callback failed after first record batch: {type(exc).__name__}"
                    )
                    write_manifest_atomic(plan.manifest_path, manifest)
    manifest["ledger"].append(
        {
            "operation": "aitable record create batches",
            "batches": record_batch_entries,
            "validation": {"success": True, "record_id_count": len(created_ids)},
        }
    )
    manifest["statistics"]["record_batch_count"] = len(record_batch_entries)
    manifest["statistics"]["written_record_count"] = len(created_ids)
    manifest["timings"]["record_write_seconds"] = monotonic() - phase_started
    set_stage(manifest, ManifestStage.WRITTEN)
    write_manifest_atomic(plan.manifest_path, manifest)

    phase_started = monotonic()
    readback_records: list[dict[str, Any]] = []
    readback_entries: list[dict[str, Any]] = []
    field_id_csv = ",".join(field_ids[name] for name in field_ids)
    for start in range(0, len(created_ids), 30):
        ids = created_ids[start : start + 30]
        query, attempts = _run_with_retry_once(
            active_runner,
            [
                "aitable",
                "record",
                "query",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--record-ids",
                ",".join(ids),
                "--field-ids",
                field_id_csv,
                "--limit",
                str(len(ids)),
            ],
            profile=target.profile,
            timeout_seconds=180.0,
        )
        records = _extract_records(query.stdout)
        readback_entries.append(
            {
                "batch": start // 30 + 1,
                "requested_record_ids": ids,
                "returned_record_count": len(records),
                "attempts": [item.to_safe_dict() for item in attempts],
            }
        )
        if not query.command_succeeded:
            manifest["ledger"].append(
                {
                    "operation": "aitable record query aggregate",
                    "batches": readback_entries,
                    "validation": {"success": False},
                }
            )
            error = query.error or StructuredError(ErrorKind.PROCESS_FAILURE, "record readback failed")
            return _finish_with_error(
                result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
            )
        readback_records.extend(records)

    verification = _verification_summary(
        plan,
        field_ids,
        created_ids,
        readback_records,
        timezone=timezone,
    )
    verification["tableId"] = table_id
    manifest["ledger"].append(
        {
            "operation": "aitable record query aggregate",
            "batches": readback_entries,
            "validation": {"success": all(verification["checks"].values()), "checks": verification["checks"]},
        }
    )
    manifest["readback"].update(
        {
            "tableId": table_id,
            "field_summary": field_summary,
            "records": verification,
            "core_verified": all(verification["checks"].values()),
        }
    )
    manifest["statistics"]["readback_record_count"] = len(readback_records)
    manifest["statistics"]["verification_checks_passed"] = sum(verification["checks"].values())
    manifest["statistics"]["verification_checks_total"] = len(verification["checks"])
    manifest["timings"]["readback_seconds"] = monotonic() - phase_started
    readback_path = _write_json_atomic(
        Path(plan.evidence_dir) / READBACK_FILENAME,
        manifest["readback"],
    )
    evidence_files.append(str(readback_path))

    if not all(verification["checks"].values()):
        diagnostic_ids = created_ids[:30]
        diagnostic, attempts = _run_with_retry_once(
            active_runner,
            [
                "aitable",
                "record",
                "query",
                "--base-id",
                base_id,
                "--table-id",
                table_id,
                "--record-ids",
                ",".join(diagnostic_ids),
                "--field-ids",
                field_id_csv,
                "--limit",
                str(len(diagnostic_ids)),
            ],
            profile=target.profile,
            timeout_seconds=180.0,
        )
        diagnostic_path = _write_json_atomic(
            Path(plan.evidence_dir) / DIAGNOSTIC_FILENAME,
            {
                "failed_checks": verification["failed_checks"],
                "detailed_plan": "compare every returned recordId and every writable field against the source-order payload",
                "diagnostic_call": _safe_run_entry("aitable record query diagnostic", attempts),
                "diagnostic_record_count": len(_extract_records(diagnostic.stdout)),
            },
        )
        evidence_files.append(str(diagnostic_path))
        manifest["ledger"].append(
            _safe_run_entry(
                "aitable record query diagnostic",
                attempts,
                triggered_by=verification["failed_checks"],
            )
        )
        error = diagnostic.error or StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "success=true did not satisfy AI Table business readback verification",
            details={"failed_checks": verification["failed_checks"]},
        )
        return _finish_with_error(
            result, manifest, plan, error, partial=True, started=started, evidence_files=evidence_files
        )

    manifest["timings"]["total_seconds"] = monotonic() - started
    if plan.attachment_plans:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "Core records verified, but attachment binary upload is unsupported_in_dws_only",
            details={
                "core_verified": True,
                "attachment_plan_count": len(plan.attachment_plans),
                "http_calls_performed": 0,
            },
        )
        set_stage(manifest, ManifestStage.PARTIAL, error=error.to_safe_dict())
    else:
        set_stage(manifest, ManifestStage.VERIFIED)
        manifest["ledger"].append(
            {
                "operation": "route_verification",
                "success": True,
                "validation": {
                    "field_checks": field_summary["checks"],
                    "record_checks": verification["checks"],
                    "direct_url_targets_business_table": f"sheetId%3D{table_id}" in direct_url,
                },
            }
        )
    write_manifest_atomic(plan.manifest_path, manifest)
    _set_result(result, manifest, evidence_files)
    return result


__all__ = ["run_aitable_route"]
