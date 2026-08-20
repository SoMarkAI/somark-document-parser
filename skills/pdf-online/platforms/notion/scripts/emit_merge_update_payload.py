from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


TABLE_PATTERN = re.compile(r"<table(?:\s[^>]*)?>.*?</table>", re.DOTALL)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_table_indentation(value: str) -> str:
    """Ignore only Notion's table-line indentation normalization."""
    return "\n".join(line.lstrip(" \t") for line in value.split("\n"))


def read_manifest(output_dir: Path) -> dict[str, Any]:
    return json.loads((output_dir / "manifest.json").read_text(encoding="utf-8-sig"))


def merged_region_count(manifest: dict[str, Any]) -> int:
    return sum(
        len(mapping["merge_degradation"].get("ranges", []))
        for mapping in manifest.get("mappings", [])
        if isinstance(mapping, dict)
        and isinstance(mapping.get("merge_degradation"), dict)
        and mapping["merge_degradation"].get("used")
    )


def build_payload(
    baseline_dir: Path,
    enhanced_dir: Path,
    current_content: str,
) -> dict[str, Any]:
    baseline_manifest = read_manifest(baseline_dir)
    enhanced_manifest = read_manifest(enhanced_dir)
    baseline_content = (baseline_dir / "page.nfm.md").read_text(encoding="utf-8")
    enhanced_content = (enhanced_dir / "page.nfm.md").read_text(encoding="utf-8")
    baseline_options = baseline_manifest.get("merge_enhancement_options", {})
    enhanced_options = enhanced_manifest.get("merge_enhancement_options", {})
    if baseline_options.get("fill_merged_cells") or baseline_options.get("color_merged_cells"):
        raise ValueError("baseline output must not contain merged-cell enhancements")
    if not (
        enhanced_options.get("fill_merged_cells")
        or enhanced_options.get("color_merged_cells")
    ):
        raise ValueError("enhanced output must enable at least one merged-cell enhancement")
    if TABLE_PATTERN.sub("<table/>", baseline_content) != TABLE_PATTERN.sub(
        "<table/>", enhanced_content
    ):
        raise ValueError("merge enhancement must not change non-table page content")
    baseline_tables = TABLE_PATTERN.findall(baseline_content)
    enhanced_tables = TABLE_PATTERN.findall(enhanced_content)
    if len(baseline_tables) != len(enhanced_tables):
        raise ValueError("baseline and enhanced outputs contain different table counts")
    current_tables = TABLE_PATTERN.findall(current_content)
    if len(current_tables) != len(baseline_tables):
        raise ValueError(
            "current Notion page contains a different table count; keep the page unchanged"
        )

    grouped: dict[str, dict[str, Any]] = {}
    indentation_normalized_count = 0
    for index, (baseline, enhanced, current) in enumerate(
        zip(baseline_tables, enhanced_tables, current_tables, strict=True),
        start=1,
    ):
        if normalize_table_indentation(current) != normalize_table_indentation(
            baseline
        ):
            raise ValueError(
                f"current Notion table {index} differs from the baseline beyond "
                "line indentation; keep the page unchanged"
            )
        if current != baseline:
            indentation_normalized_count += 1
        entry = grouped.setdefault(
            current,
            {"changed_states": set(), "enhanced_values": set(), "count": 0},
        )
        entry["changed_states"].add(baseline != enhanced)
        entry["enhanced_values"].add(enhanced)
        entry["count"] += 1

    updates: list[dict[str, Any]] = []
    changed_table_count = 0
    for current, entry in grouped.items():
        if len(entry["changed_states"]) != 1:
            raise ValueError(
                "identical current Notion tables map to both changed and unchanged "
                "variants; keep the page unchanged"
            )
        if not next(iter(entry["changed_states"])):
            continue
        if len(entry["enhanced_values"]) != 1:
            raise ValueError(
                "one current Notion table maps to multiple enhanced variants; "
                "keep the page unchanged"
            )
        updates.append(
            {
                "old_str": current,
                "new_str": next(iter(entry["enhanced_values"])),
                "replace_all_matches": entry["count"] > 1,
            }
        )
        changed_table_count += entry["count"]
    return {
        "content_updates": updates,
        "changed_table_count": changed_table_count,
        "merged_region_count": merged_region_count(enhanced_manifest),
        "fill_merged_cells": bool(enhanced_options.get("fill_merged_cells")),
        "color_merged_cells": bool(enhanced_options.get("color_merged_cells")),
        "notion_indentation_normalized_table_count": indentation_normalized_count,
        "baseline_content_sha256": digest(baseline_content),
        "enhanced_content_sha256": digest(enhanced_content),
        "current_content_sha256": digest(current_content),
    }


def read_current_content(args: argparse.Namespace) -> str:
    if args.current_content_file is not None:
        return args.current_content_file.read_text(encoding="utf-8-sig")
    raw = sys.stdin.readline()
    if not raw:
        raise ValueError(
            "expected one JSON-encoded line containing the current Notion page content"
        )
    value = json.loads(raw)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    raise ValueError("current Notion content must be a JSON string or an object with text")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit ASCII-safe targeted Notion table updates for merge enhancement."
    )
    parser.add_argument("baseline_dir", type=Path)
    parser.add_argument("enhanced_dir", type=Path)
    current_source = parser.add_mutually_exclusive_group(required=True)
    current_source.add_argument("--current-content-file", type=Path)
    current_source.add_argument("--current-content-json-stdin", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build_payload(
                args.baseline_dir,
                args.enhanced_dir,
                read_current_content(args),
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
