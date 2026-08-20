#!/usr/bin/env python3
"""Emit ASCII-only Notion MCP payloads from a validated database plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PLAN_FORMAT = "somark-to-notion-database-plan-v1"
DEFAULT_MAX_RECORDS_PER_BATCH = 100
PROPERTY_TYPES = {"title", "rich_text", "number", "date", "select"}


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise ValueError(f"database plan format must be {PLAN_FORMAT!r}")
    if not plan.get("validation", {}).get("valid"):
        raise ValueError("database plan is not prewrite-validated")
    properties = plan.get("properties")
    records = plan.get("records")
    if not isinstance(properties, list) or not isinstance(records, list):
        raise ValueError("database plan must contain properties and records arrays")
    names = [prop.get("name") for prop in properties]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise ValueError("database property names must be nonempty and unique")
    if sum(prop.get("type") == "title" for prop in properties) != 1:
        raise ValueError("database plan must contain exactly one title property")
    unsupported = sorted({prop.get("type") for prop in properties} - PROPERTY_TYPES)
    if unsupported:
        raise ValueError("unsupported property types: " + ", ".join(map(str, unsupported)))
    return plan


def schema_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "database_name": plan["database_name"],
        "properties": [
            {
                "name": prop["name"],
                "type": prop["type"],
                **({"options": prop["options"]} if prop.get("type") == "select" else {}),
            }
            for prop in plan["properties"]
        ],
    }


def record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_row_number": record["source_row_number"],
        "properties": record["converted_values"],
    }


def build_payload(plan: dict[str, Any], *, max_records_per_batch: int) -> dict[str, Any]:
    if max_records_per_batch < 1:
        raise ValueError("max_records_per_batch must be positive")
    records = [record_payload(record) for record in plan["records"]]
    batches = [
        records[start : start + max_records_per_batch]
        for start in range(0, len(records), max_records_per_batch)
    ]
    configure_dsl = plan["default_table_view"].get("configure_dsl")
    if not isinstance(configure_dsl, str) or not configure_dsl.strip():
        raise ValueError("database plan must contain a complete configure_dsl")
    return {
        "format": "somark-to-notion-database-mcp-payload-v1",
        "database": schema_payload(plan),
        "database_suitability": plan.get(
            "database_suitability",
            {
                "recommended": True,
                "requires_confirmation": False,
                "can_force_continue": True,
                "decision": "proceed",
                "risks": [],
            },
        ),
        "warnings": plan.get("warnings_and_fallbacks", plan.get("warnings", [])),
        "record_submission": {
            "strategy": "single_batch" if len(batches) <= 1 else "deterministic_batches",
            "batch_size_limit": max_records_per_batch,
            "batch_count": len(batches),
            "total_records": len(records),
            "reuse_created_database_on_failure": True,
            "batches": [
                {"batch_number": index, "records": batch}
                for index, batch in enumerate(batches, start=1)
            ],
        },
        "default_table_view": plan["default_table_view"],
        "configure_dsl": configure_dsl,
        "readback_acceptance": {
            "schema_property_names": [prop["name"] for prop in plan["properties"]],
            "expected_record_count": len(records),
            "first_source_row_number": records[0]["source_row_number"] if records else None,
            "last_source_row_number": records[-1]["source_row_number"] if records else None,
            "expected_first_record": records[0] if records else None,
            "expected_last_record": records[-1] if records else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit ASCII-only schema, record batches, view configuration, and readback checks."
    )
    parser.add_argument("database_plan", type=Path)
    parser.add_argument("--max-records-per-batch", type=int, default=DEFAULT_MAX_RECORDS_PER_BATCH)
    args = parser.parse_args()
    payload = build_payload(
        load_plan(args.database_plan), max_records_per_batch=args.max_records_per_batch
    )
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
