"""Typed, route-private models for the DingTalk AI Table adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import redact_sensitive


@dataclass(frozen=True)
class AitableFieldPlan:
    """One source field and its conservative DingTalk representation."""

    source_name: str
    source_type: str
    target_type: str
    config: Mapping[str, Any]
    create_order: int
    primary: bool = False
    record_writable: bool = True
    downgrade_reason: str | None = None

    def creation_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fieldName": self.source_name,
            "type": self.target_type,
        }
        if self.config:
            payload["config"] = dict(self.config)
        return payload

    def to_safe_dict(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "source_name": self.source_name,
                "source_type": self.source_type,
                "target_type": self.target_type,
                "config": dict(self.config),
                "create_order": self.create_order,
                "primary": self.primary,
                "record_writable": self.record_writable,
                "downgrade_reason": self.downgrade_reason,
            }
        )


@dataclass(frozen=True)
class AitableAttachmentPlan:
    """A DWS-only attachment preparation plan; it never performs HTTP PUT."""

    field_name: str
    record_index: int
    source_value: Any
    local_path: str | None
    file_name: str | None
    size: int | None
    mime_type: str | None
    status: str = "unsupported_in_dws_only"

    def to_safe_dict(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "field_name": self.field_name,
                "record_index": self.record_index,
                "source_value": self.source_value,
                "local_path": self.local_path,
                "file_name": self.file_name,
                "size": self.size,
                "mime_type": self.mime_type,
                "status": self.status,
                "binary_upload_performed": False,
            }
        )


@dataclass(frozen=True)
class AitablePlan:
    """Stable local plan produced before any DingTalk write is considered."""

    title: str
    table_name: str
    records_path: str
    mapping_path: str
    evidence_dir: str
    manifest_path: str
    field_plan_path: str
    record_plan_path: str
    degradation_plan_path: str
    field_plans: tuple[AitableFieldPlan, ...]
    normalized_records: tuple[Mapping[str, Any], ...]
    attachment_plans: tuple[AitableAttachmentPlan, ...]
    route_eligible: bool
    recommended_route: str
    routing_reasons: tuple[str, ...]
    statistics: Mapping[str, Any]
    degradations: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]

    @property
    def field_payload(self) -> list[dict[str, Any]]:
        return [field.creation_payload() for field in self.field_plans]

    def to_safe_dict(self) -> dict[str, Any]:
        return redact_sensitive(
            {
                "title": self.title,
                "table_name": self.table_name,
                "records_path": self.records_path,
                "mapping_path": self.mapping_path,
                "evidence_dir": self.evidence_dir,
                "manifest_path": self.manifest_path,
                "field_plan_path": self.field_plan_path,
                "record_plan_path": self.record_plan_path,
                "degradation_plan_path": self.degradation_plan_path,
                "field_plans": [item.to_safe_dict() for item in self.field_plans],
                "normalized_records": [dict(item) for item in self.normalized_records],
                "attachment_plans": [item.to_safe_dict() for item in self.attachment_plans],
                "route_eligible": self.route_eligible,
                "recommended_route": self.recommended_route,
                "routing_reasons": list(self.routing_reasons),
                "statistics": dict(self.statistics),
                "degradations": [dict(item) for item in self.degradations],
                "warnings": list(self.warnings),
            }
        )


__all__ = [
    "AitableAttachmentPlan",
    "AitableFieldPlan",
    "AitablePlan",
]
