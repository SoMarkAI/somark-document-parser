"""One public lifecycle for explicit SoMark artifacts and DingTalk route execution."""

from __future__ import annotations

from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Callable, Mapping, Sequence

from .artifacts import RouteName, RouteResult, RouteTarget, SourceArtifacts
from .dws_runner import DwsRunner
from .errors import ErrorKind, StructuredError, redact_sensitive
from .manifest import ManifestStage, new_manifest, read_manifest, set_stage, write_manifest_atomic


PUBLISH_MANIFEST_FILENAME = "publish_manifest.json"
PUBLISH_SCHEMA_VERSION = 1
EventSink = Callable[[Mapping[str, Any]], None]


class PublishError(RuntimeError):
    """A safe lifecycle error that must not trigger a second target creation."""


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_paths(paths: Sequence[Path]) -> str:
    digest = sha256()
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _existing_file(value: str | Path | None, label: str) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist or is not a file: {path}")
    return path


def _existing_dir(value: str | Path | None, label: str) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"{label} does not exist or is not a directory: {path}")
    return path


def _stdout_sink(event: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class _EventJournal:
    def __init__(
        self,
        path: Path,
        manifest: dict[str, Any],
        sink: EventSink,
    ) -> None:
        self.path = path
        self.manifest = manifest
        self.sink = sink
        publish_meta = self.manifest["readback"].setdefault("publish", {})
        publish_meta.setdefault("events", [])

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.manifest["readback"]["publish"]["events"]

    def save(self) -> None:
        write_manifest_atomic(self.path, self.manifest)

    def emit(self, event_name: str, **payload: Any) -> bool:
        event = redact_sensitive(
            {
                "seq": len(self.events) + 1,
                "event": event_name,
                "route": self.manifest["route"],
                **payload,
            }
        )
        receipt = {"payload": event, "delivered": False}
        self.events.append(receipt)
        self.save()
        try:
            self.sink(event)
        except Exception as exc:
            self.manifest["warnings"].append(
                f"NDJSON event delivery failed for seq={event['seq']}: {type(exc).__name__}"
            )
            self.save()
            return False
        receipt["delivered"] = True
        self.save()
        return True

    def replay_undelivered(self) -> int:
        delivered = 0
        for receipt in self.events:
            if receipt.get("delivered") is True:
                continue
            payload = receipt.get("payload")
            if not isinstance(payload, Mapping):
                continue
            self.sink(dict(payload))
            receipt["delivered"] = True
            delivered += 1
            self.save()
        return delivered


def _explicit_artifacts(
    route: RouteName,
    source: Path | None,
    markdown: Path,
    structured: Path,
    assets: Path | None,
) -> SourceArtifacts:
    evidence = tuple(str(path) for path in (source, markdown, structured) if path is not None)
    if source is not None:
        source_hash = _hash_file(source)
    elif route is RouteName.AITABLE:
        source_hash = _hash_file(structured)
    else:
        source_hash = _hash_paths((markdown, structured))
    return SourceArtifacts(
        source_path=str(source) if source is not None else None,
        source_hash=source_hash,
        markdown_path=str(markdown),
        json_path=str(structured),
        assets_dir=str(assets) if assets is not None else None,
        evidence_files=evidence,
    )


def _prepare_artifacts(
    *,
    route: RouteName,
    source: Path | None,
    markdown_path: str | Path | None,
    json_path: str | Path | None,
    assets_dir: str | Path | None,
) -> tuple[SourceArtifacts, int, float, str | None]:
    markdown = _existing_file(markdown_path, "explicit Markdown artifact")
    structured = _existing_file(json_path, "explicit JSON artifact")
    assets = _existing_dir(assets_dir, "explicit asset directory")
    if (markdown is None) != (structured is None):
        raise ValueError("explicit artifact mode requires both --markdown and --json")
    if markdown is not None and structured is not None:
        return _explicit_artifacts(route, source, markdown, structured, assets), 0, 0.0, None
    raise ValueError(
        "DingTalk publishing requires the exact Markdown and JSON artifacts from the "
        "official somark-document-parser Skill; source-only publishing is not supported"
    )


def _route_module(route: RouteName) -> Any:
    module_names = {
        RouteName.DOCUMENT: "somark_dingtalk.document",
        RouteName.SHEET: "somark_dingtalk.sheet_route",
        RouteName.AITABLE: "somark_dingtalk.aitable_executor",
    }
    return importlib.import_module(module_names[route])


def _business_call_count(value: Any) -> int:
    count = 0

    def visit(item: Any) -> None:
        nonlocal count
        if isinstance(item, Mapping):
            command = item.get("command")
            if (
                isinstance(command, (list, tuple))
                and command
                and Path(str(command[0])).name.casefold() in {"dws", "dws.exe"}
                and "--version" not in command
                and "version" not in command[1:2]
                and "exit_code" in item
            ):
                count += 1
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return count


def _compact_result(result: RouteResult, route_manifest: str | None) -> dict[str, Any]:
    return redact_sensitive(
        {
            "route": result.route.value,
            "stage": result.stage,
            "target": dict(result.target),
            "direct_url": result.direct_url,
            "timings": dict(result.timings),
            "statistics": dict(result.statistics),
            "degradations": list(result.degradations),
            "warnings": list(result.warnings),
            "error": result.error,
            "route_manifest": route_manifest,
        }
    )


def _result_from_receipt(receipt: Mapping[str, Any]) -> RouteResult:
    route = RouteName(str(receipt["route"]))
    return RouteResult(
        route=route,
        stage=str(receipt.get("stage") or ManifestStage.FAILED.value),
        target=dict(receipt.get("target") or {}),
        direct_url=receipt.get("direct_url") if isinstance(receipt.get("direct_url"), str) else None,
        timings=dict(receipt.get("timings") or {}),
        statistics=dict(receipt.get("statistics") or {}),
        degradations=list(receipt.get("degradations") or []),
        warnings=list(receipt.get("warnings") or []),
        error=receipt.get("error") if isinstance(receipt.get("error"), Mapping) else None,
        evidence_files=[str(receipt["route_manifest"])] if receipt.get("route_manifest") else [],
    )


def _route_manifest_path(route: RouteName, evidence_dir: Path, result: RouteResult) -> str | None:
    names = {
        RouteName.DOCUMENT: "document_route_manifest.json",
        RouteName.SHEET: "sheet_manifest.json",
        RouteName.AITABLE: "aitable_route_manifest.json",
    }
    expected = evidence_dir / names[route]
    if expected.is_file():
        return str(expected)
    return next(
        (
            path
            for path in result.evidence_files
            if Path(path).name.casefold().endswith("manifest.json")
        ),
        None,
    )


def publish(
    *,
    source: str | Path | None,
    route: str | RouteName,
    title: str,
    evidence_dir: str | Path,
    profile: str | None = None,
    mode: str = "fast",
    execute: bool = True,
    markdown_path: str | Path | None = None,
    json_path: str | Path | None = None,
    assets_dir: str | Path | None = None,
    dws_runner: DwsRunner | None = None,
    event_sink: EventSink | None = None,
    timezone: str = "Asia/Shanghai",
    table_index: int | None = None,
    preview_first: bool = False,
) -> RouteResult:
    """Consume one explicit artifact pair, lazily load one route, and emit a stable lifecycle."""

    route_name = route if isinstance(route, RouteName) else RouteName(str(route))
    if mode not in {"fast", "strict"}:
        raise ValueError("mode must be 'fast' or 'strict'")
    if table_index is not None and route_name is not RouteName.AITABLE:
        raise ValueError("table_index is supported only for the AI Table route")
    if preview_first and route_name is not RouteName.SHEET:
        raise ValueError("preview_first is supported only for the sheet route")
    if not title.strip():
        raise ValueError("title must not be empty")
    evidence = Path(evidence_dir).expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    source_path = _existing_file(source, "source")
    explicit_md = _existing_file(markdown_path, "explicit Markdown artifact")
    explicit_json = _existing_file(json_path, "explicit JSON artifact")
    hash_inputs = [item for item in (source_path, explicit_md, explicit_json) if item is not None]
    if not hash_inputs:
        raise ValueError("publish requires a source or explicit artifacts")
    initial_hash = _hash_file(source_path) if source_path is not None else _hash_paths(hash_inputs)
    manifest_path = evidence / PUBLISH_MANIFEST_FILENAME
    manifest = new_manifest(
        route=route_name.value,
        source=str(source_path) if source_path is not None else None,
        source_hash=initial_hash,
        somark_artifacts={},
        dws_cli_version="1.0.57",
        target={"title": title, "direct_url": None, "table_index": table_index},
    )
    manifest["readback"]["publish"] = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "mode": mode,
        "execute": bool(execute),
        "profile": "[EXPLICIT]" if profile else None,
        "preview_first": bool(preview_first),
        "postprocess_pending": False,
        "events": [],
        "terminal_result": None,
    }
    manifest["statistics"].update(
        {"somark_parse_calls": 0, "dws_business_call_count": 0}
    )
    journal = _EventJournal(manifest_path, manifest, event_sink or _stdout_sink)
    journal.save()
    task_started = monotonic()
    parse_started = monotonic()
    explicit_mode = explicit_md is not None or explicit_json is not None
    journal.emit(
        "parse_started",
        source=str(source_path) if source_path is not None else None,
        explicit_artifacts=explicit_mode,
    )

    try:
        artifacts, parse_calls, parser_elapsed, index_path = _prepare_artifacts(
            route=route_name,
            source=source_path,
            markdown_path=str(explicit_md) if explicit_md is not None else None,
            json_path=str(explicit_json) if explicit_json is not None else None,
            assets_dir=assets_dir,
        )
    except Exception as exc:
        error = StructuredError(
            ErrorKind.PROCESS_FAILURE if isinstance(exc, PublishError) else ErrorKind.INVALID_ARGUMENT,
            str(exc),
            details={"phase": "parse"},
        )
        manifest["timings"]["somark_seconds"] = monotonic() - parse_started
        manifest["timings"]["total_seconds"] = monotonic() - task_started
        set_stage(manifest, ManifestStage.FAILED, error=error.to_safe_dict())
        journal.save()
        journal.emit("failed", stage=ManifestStage.FAILED.value, error=error.to_safe_dict())
        return RouteResult(route=route_name, stage=ManifestStage.FAILED.value, error=error.to_safe_dict())

    parse_completed_at = monotonic()
    manifest["source"] = artifacts.source_path
    manifest["source_hash"] = artifacts.source_hash
    manifest["somark_artifacts"] = artifacts.to_manifest_dict()
    manifest["statistics"]["somark_parse_calls"] = parse_calls
    manifest["timings"]["somark_seconds"] = (
        0.0
        if parse_calls == 0
        else parser_elapsed if parser_elapsed > 0 else parse_completed_at - parse_started
    )
    journal.save()
    journal.emit(
        "parse_completed",
        parse_calls=parse_calls,
        elapsed_seconds=manifest["timings"]["somark_seconds"],
        markdown_path=artifacts.markdown_path,
        json_path=artifacts.json_path,
        assets_dir=artifacts.assets_dir,
        results_index=index_path,
    )

    preview_started: float | None = None
    preview_seen = False
    postprocess_seen = False
    deferred_document_preview: dict[str, Any] | None = None

    def on_plan(event: Mapping[str, Any]) -> None:
        elapsed = monotonic() - parse_completed_at
        manifest["timings"]["local_orchestration_seconds"] = elapsed
        journal.emit(
            "plan_completed",
            elapsed_seconds=elapsed,
            route_planning_seconds=event.get("planning_seconds"),
            route_manifest=event.get("manifest"),
        )

    def emit_preview(event: Mapping[str, Any]) -> None:
        nonlocal preview_seen
        preview_seen = True
        direct_url = event.get("direct_url")
        target_fields = {
            key: event.get(key)
            for key in ("nodeId", "sheetIds", "baseId", "tableId")
            if event.get(key) is not None
        }
        record_ids = event.get("recordIds")
        manifest["target"].update({**target_fields, "direct_url": direct_url})
        manifest["timings"]["time_to_preview_seconds"] = monotonic() - task_started
        journal.save()
        business_id = event.get("tableId") or event.get("nodeId") or event.get("baseId")
        journal.emit(
            "preview_ready",
            stage=event.get("stage") or ManifestStage.WRITTEN.value,
            business_id=business_id,
            direct_url=direct_url,
            postprocess_pending=bool(event.get("postprocess_pending")),
            recordIds=list(record_ids) if isinstance(record_ids, (list, tuple)) else None,
            **target_fields,
        )

    def on_preview(event: Mapping[str, Any]) -> None:
        nonlocal preview_started, postprocess_seen, deferred_document_preview
        preview_started = monotonic()
        if route_name is RouteName.DOCUMENT:
            deferred_document_preview = dict(event)
        else:
            emit_preview(event)
        if preview_first and route_name is RouteName.SHEET:
            manifest["readback"]["publish"]["postprocess_pending"] = bool(
                event.get("postprocess_pending")
            )
            journal.save()
            return
        postprocess_seen = True
        journal.emit(
            "postprocess_started",
            pending=bool(event.get("postprocess_pending")),
        )

    target = RouteTarget(
        route=route_name,
        title=title,
        evidence_dir=str(evidence),
        profile=profile,
        table_index=table_index,
    )
    try:
        module = _route_module(route_name)
        common = {
            "runner": dws_runner,
            "execute": execute,
            "plan_callback": on_plan,
            "preview_callback": on_preview,
        }
        if route_name is RouteName.DOCUMENT:
            result = module.run_document_route(
                artifacts,
                target,
                verify=(mode == "strict"),
                **common,
            )
        elif route_name is RouteName.SHEET:
            result = module.run_sheet_route(
                artifacts,
                target,
                enhance=not preview_first,
                fast_mode=(mode == "fast"),
                **common,
            )
        else:
            result = module.run_aitable_route(
                artifacts,
                target,
                timezone=timezone,
                fast_mode=(mode == "fast"),
                **common,
            )
    except Exception as exc:
        error = StructuredError(
            ErrorKind.PROCESS_FAILURE,
            str(exc),
            details={"phase": "route"},
        )
        result = RouteResult(
            route=route_name,
            stage=ManifestStage.FAILED.value,
            error=error.to_safe_dict(),
        )

    finished = monotonic()
    if postprocess_seen:
        manifest["timings"]["postprocess_seconds"] = finished - (preview_started or finished)
        journal.emit(
            (
                "postprocess_interrupted"
                if result.stage in {ManifestStage.FAILED.value, ManifestStage.PARTIAL.value}
                else "postprocess_completed"
            ),
            elapsed_seconds=manifest["timings"]["postprocess_seconds"],
            stage=result.stage,
            resumable=(
                route_name is RouteName.DOCUMENT
                and result.stage == ManifestStage.PARTIAL.value
                and bool(result.target.get("nodeId"))
            ),
        )
    elif execute and preview_seen:
        manifest["timings"]["postprocess_seconds"] = 0.0
    if (
        route_name is RouteName.DOCUMENT
        and deferred_document_preview is not None
        and result.stage not in {ManifestStage.FAILED.value, ManifestStage.PARTIAL.value}
    ):
        deferred_document_preview["postprocess_pending"] = False
        emit_preview(deferred_document_preview)
    manifest["timings"]["total_seconds"] = finished - task_started
    manifest["statistics"]["dws_business_call_count"] = _business_call_count(result.ledger)
    manifest["statistics"]["result_stage"] = result.stage
    manifest["target"].update({**dict(result.target), "direct_url": result.direct_url})
    route_manifest = _route_manifest_path(route_name, evidence, result)
    terminal_receipt = _compact_result(result, route_manifest)
    manifest["readback"]["publish"]["terminal_result"] = terminal_receipt
    preview_paused = bool(
        preview_first
        and route_name is RouteName.SHEET
        and result.stage == ManifestStage.WRITTEN.value
        and manifest["readback"]["publish"].get("postprocess_pending")
    )
    if preview_paused:
        terminal_name = "preview_paused"
        terminal_stage = ManifestStage.WRITTEN
    elif result.stage == ManifestStage.FAILED.value:
        terminal_name = "failed"
        terminal_stage = ManifestStage.FAILED
    elif result.stage == ManifestStage.PARTIAL.value:
        terminal_name = "partial"
        terminal_stage = ManifestStage.PARTIAL
    else:
        terminal_name = "completed"
        terminal_stage = (
            ManifestStage.VERIFIED
            if result.stage == ManifestStage.VERIFIED.value
            else ManifestStage.WRITTEN
        )
    set_stage(manifest, terminal_stage, error=result.error)
    journal.save()
    journal.emit(
        terminal_name,
        stage=result.stage,
        direct_url=(
            None
            if route_name is RouteName.DOCUMENT
            and terminal_name in {"failed", "partial"}
            else result.direct_url
        ),
        somark_parse_calls=manifest["statistics"]["somark_parse_calls"],
        dws_business_call_count=manifest["statistics"]["dws_business_call_count"],
        total_seconds=manifest["timings"]["total_seconds"],
        plan_only=not execute,
        error=result.error,
    )
    result.timings = {**manifest["timings"], **result.timings}
    result.statistics = {**manifest["statistics"], **result.statistics}
    result.evidence_files = list(
        dict.fromkeys([*result.evidence_files, str(manifest_path)])
    )
    return result


def resume(
    manifest_path: str | Path,
    *,
    profile: str | None = None,
    dws_runner: DwsRunner | None = None,
    event_sink: EventSink | None = None,
) -> RouteResult:
    """Replay events and continue deferred work on the existing target."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = read_manifest(path)
    publish_meta = manifest.get("readback", {}).get("publish")
    if not isinstance(publish_meta, Mapping):
        raise ValueError("manifest is not a somark-to-dingtalk publish manifest")
    journal = _EventJournal(path, manifest, event_sink or _stdout_sink)
    journal.replay_undelivered()
    receipt = publish_meta.get("terminal_result")
    if not isinstance(receipt, Mapping):
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "the task stopped before a terminal receipt; refusing to parse or create a replacement target",
        )
        return RouteResult(
            route=RouteName(str(manifest["route"])),
            stage=ManifestStage.PARTIAL.value,
            target=dict(manifest.get("target") or {}),
            direct_url=(manifest.get("target") or {}).get("direct_url"),
            error=error.to_safe_dict(),
            evidence_files=[str(path)],
        )
    result = _result_from_receipt(receipt)
    result.statistics["somark_parse_calls_on_resume"] = 0
    result.statistics["target_create_calls_on_resume"] = 0
    result.evidence_files = list(dict.fromkeys([*result.evidence_files, str(path)]))
    route = RouteName(str(manifest["route"]))
    sheet_postprocess_pending = bool(
        route is RouteName.SHEET
        and result.stage == ManifestStage.WRITTEN.value
        and publish_meta.get("preview_first") is True
        and publish_meta.get("postprocess_pending") is True
    )
    if sheet_postprocess_pending:
        if profile is None or not profile.strip():
            error = StructuredError(
                ErrorKind.PROFILE_REQUIRED,
                "spreadsheet enhancement requires the original explicit DWS profile",
                hint="rerun resume with --profile <the same profile used for publish>",
            )
            journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
            result.error = error.to_safe_dict()
            return result
        stored = manifest.get("somark_artifacts")
        title = str((manifest.get("target") or {}).get("title") or "").strip()
        if not isinstance(stored, Mapping) or not title:
            error = StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "publish manifest does not contain the spreadsheet artifacts and title required for enhancement",
            )
            journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
            result.error = error.to_safe_dict()
            return result
        source = SourceArtifacts(
            source_path=stored.get("source_path") if isinstance(stored.get("source_path"), str) else None,
            source_hash=str(stored.get("source_hash") or manifest["source_hash"]),
            markdown_path=stored.get("markdown_path") if isinstance(stored.get("markdown_path"), str) else None,
            json_path=stored.get("json_path") if isinstance(stored.get("json_path"), str) else None,
            assets_dir=stored.get("assets_dir") if isinstance(stored.get("assets_dir"), str) else None,
            evidence_files=tuple(
                str(item) for item in (stored.get("evidence_files") or []) if isinstance(item, str)
            ),
        )
        target = RouteTarget(
            route=route,
            title=title,
            evidence_dir=str(path.parent),
            profile=profile,
        )
        result_target = result.target
        manifest_target = manifest.get("target") or {}
        node_id = str(
            result_target.get("node_id")
            or result_target.get("nodeId")
            or manifest_target.get("node_id")
            or manifest_target.get("nodeId")
            or ""
        )
        raw_sheet_ids = (
            result_target.get("sheet_ids")
            or result_target.get("sheetIds")
            or manifest_target.get("sheet_ids")
            or manifest_target.get("sheetIds")
            or []
        )
        sheet_ids = [str(item) for item in raw_sheet_ids] if isinstance(raw_sheet_ids, (list, tuple)) else []
        if not node_id or not sheet_ids:
            error = StructuredError(
                ErrorKind.BUSINESS_VALIDATION,
                "publish manifest does not contain the existing workbook and worksheet identifiers",
            )
            journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
            result.error = error.to_safe_dict()
            return result
        result.target.update({"node_id": node_id, "sheet_ids": sheet_ids})
        result.direct_url = result.direct_url or manifest_target.get("direct_url")
        journal.emit(
            "resume_started",
            stage=result.stage,
            nodeId=node_id,
            somark_parse_calls=0,
            target_create_calls=0,
        )
        journal.emit("postprocess_started", pending=True)
        active_runner = dws_runner or DwsRunner()
        try:
            _version, version_error = active_runner.read_version()
            if version_error:
                raise PublishError(version_error.message)
            module = _route_module(route)
            plan = module.plan_sheet_route(source, target)
            resumed = module.enhance_sheet_route(
                plan,
                target,
                runner=active_runner,
                node_id=node_id,
                sheet_ids=sheet_ids,
                base_result=result,
                fast_mode=(publish_meta.get("mode") == "fast"),
            )
        except Exception as exc:
            error = StructuredError(
                ErrorKind.PROCESS_FAILURE,
                str(exc),
                details={"phase": "resume_sheet_postprocess"},
            )
            resumed = RouteResult(
                route=route,
                stage=ManifestStage.PARTIAL.value,
                target=dict(result.target),
                direct_url=result.direct_url,
                error=error.to_safe_dict(),
            )
        journal.emit(
            "postprocess_interrupted"
            if resumed.stage in {ManifestStage.FAILED.value, ManifestStage.PARTIAL.value}
            else "postprocess_completed",
            stage=resumed.stage,
            resumable=False,
        )
        route_manifest = _route_manifest_path(route, path.parent, resumed)
        publish_meta["terminal_result"] = _compact_result(resumed, route_manifest)
        publish_meta["postprocess_pending"] = False
        manifest["target"].update({**dict(resumed.target), "direct_url": resumed.direct_url})
        manifest["statistics"]["somark_parse_calls_on_resume"] = 0
        manifest["statistics"]["target_create_calls_on_resume"] = 0
        resume_business_call_count = _business_call_count(resumed.ledger)
        manifest["statistics"]["dws_business_call_count_on_resume"] = resume_business_call_count
        manifest["statistics"]["dws_business_call_count"] = int(
            manifest["statistics"].get("dws_business_call_count") or 0
        ) + resume_business_call_count
        manifest["statistics"]["result_stage"] = resumed.stage
        terminal_stage = (
            ManifestStage.PARTIAL
            if resumed.stage == ManifestStage.PARTIAL.value
            else ManifestStage.FAILED
            if resumed.stage == ManifestStage.FAILED.value
            else ManifestStage.VERIFIED
            if resumed.stage == ManifestStage.VERIFIED.value
            else ManifestStage.WRITTEN
        )
        set_stage(manifest, terminal_stage, error=resumed.error)
        journal.save()
        journal.emit(
            "resume_partial" if resumed.stage == ManifestStage.PARTIAL.value else "resume_completed",
            stage=resumed.stage,
            nodeId=node_id,
            direct_url=resumed.direct_url,
            somark_parse_calls=0,
            target_create_calls=0,
            error=resumed.error,
        )
        resumed.statistics = {
            **resumed.statistics,
            "somark_parse_calls_on_resume": 0,
            "target_create_calls_on_resume": 0,
        }
        resumed.evidence_files = list(
            dict.fromkeys([*resumed.evidence_files, str(path)])
        )
        return resumed

    if result.stage != ManifestStage.PARTIAL.value:
        return result

    if route is not RouteName.DOCUMENT:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "partial continuation is currently supported only for the document route",
        )
        journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
        result.error = error.to_safe_dict()
        return result
    if profile is None or not profile.strip():
        error = StructuredError(
            ErrorKind.PROFILE_REQUIRED,
            "partial document continuation requires the original explicit DWS profile",
            hint="rerun resume with --profile <the same profile used for publish>",
        )
        journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
        result.error = error.to_safe_dict()
        return result

    stored = manifest.get("somark_artifacts")
    if not isinstance(stored, Mapping):
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "publish manifest does not contain resumable SoMark artifacts",
        )
        journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
        result.error = error.to_safe_dict()
        return result
    title = str((manifest.get("target") or {}).get("title") or "").strip()
    if not title:
        error = StructuredError(
            ErrorKind.BUSINESS_VALIDATION,
            "publish manifest does not contain the original target title",
        )
        journal.emit("resume_blocked", stage=result.stage, error=error.to_safe_dict())
        result.error = error.to_safe_dict()
        return result

    source = SourceArtifacts(
        source_path=stored.get("source_path") if isinstance(stored.get("source_path"), str) else None,
        source_hash=str(stored.get("source_hash") or manifest["source_hash"]),
        markdown_path=stored.get("markdown_path") if isinstance(stored.get("markdown_path"), str) else None,
        json_path=stored.get("json_path") if isinstance(stored.get("json_path"), str) else None,
        assets_dir=stored.get("assets_dir") if isinstance(stored.get("assets_dir"), str) else None,
        evidence_files=tuple(
            str(item) for item in (stored.get("evidence_files") or []) if isinstance(item, str)
        ),
    )
    target = RouteTarget(
        route=route,
        title=title,
        evidence_dir=str(path.parent),
        profile=profile,
    )
    journal.emit(
        "resume_started",
        stage=result.stage,
        nodeId=(manifest.get("target") or {}).get("nodeId"),
        somark_parse_calls=0,
        target_create_calls=0,
    )
    route_manifest_before = path.parent / "document_route_manifest.json"
    prior_business_call_count = 0
    try:
        prior_route_manifest = read_manifest(route_manifest_before)
        prior_business_call_count = _business_call_count(
            prior_route_manifest.get("ledger") or []
        )
    except (FileNotFoundError, OSError, ValueError):
        prior_business_call_count = 0

    reused_preview: dict[str, Any] = {}

    def on_preview(event: Mapping[str, Any]) -> None:
        reused_preview.update(dict(event))
        journal.emit(
            "resume_target_reused",
            stage=event.get("stage"),
            nodeId=event.get("nodeId"),
            direct_url=event.get("direct_url"),
            duplicate_create_avoided=True,
        )

    try:
        module = _route_module(route)
        resumed = module.run_document_route(
            source,
            target,
            runner=dws_runner,
            execute=True,
            verify=(publish_meta.get("mode") == "strict"),
            plan_callback=lambda _event: None,
            preview_callback=on_preview,
        )
    except Exception as exc:
        error = StructuredError(
            ErrorKind.PROCESS_FAILURE,
            str(exc),
            details={"phase": "resume_document"},
        )
        resumed = RouteResult(
            route=route,
            stage=ManifestStage.PARTIAL.value,
            target=dict(manifest.get("target") or {}),
            direct_url=(manifest.get("target") or {}).get("direct_url"),
            error=error.to_safe_dict(),
        )

    route_manifest = _route_manifest_path(route, path.parent, resumed)
    publish_meta["terminal_result"] = _compact_result(resumed, route_manifest)
    manifest["target"].update({**dict(resumed.target), "direct_url": resumed.direct_url})
    manifest["statistics"]["somark_parse_calls_on_resume"] = 0
    manifest["statistics"]["target_create_calls_on_resume"] = 0
    total_business_call_count = _business_call_count(resumed.ledger)
    manifest["statistics"]["dws_business_call_count"] = total_business_call_count
    manifest["statistics"]["dws_business_call_count_on_resume"] = max(
        0, total_business_call_count - prior_business_call_count
    )
    manifest["statistics"]["result_stage"] = resumed.stage
    terminal_stage = (
        ManifestStage.PARTIAL
        if resumed.stage == ManifestStage.PARTIAL.value
        else ManifestStage.FAILED
        if resumed.stage == ManifestStage.FAILED.value
        else ManifestStage.VERIFIED
        if resumed.stage == ManifestStage.VERIFIED.value
        else ManifestStage.WRITTEN
    )
    set_stage(manifest, terminal_stage, error=resumed.error)
    journal.save()
    journal.emit(
        "resume_partial" if resumed.stage == ManifestStage.PARTIAL.value else "resume_completed",
        stage=resumed.stage,
        nodeId=resumed.target.get("nodeId"),
        direct_url=(None if resumed.stage == ManifestStage.PARTIAL.value else resumed.direct_url),
        somark_parse_calls=0,
        target_create_calls=0,
        error=resumed.error,
    )
    resumed.statistics = {
        **resumed.statistics,
        "somark_parse_calls_on_resume": 0,
        "target_create_calls_on_resume": 0,
    }
    resumed.evidence_files = list(
        dict.fromkeys([*resumed.evidence_files, str(path)])
    )
    return resumed


__all__ = ["PUBLISH_MANIFEST_FILENAME", "PublishError", "publish", "resume"]
