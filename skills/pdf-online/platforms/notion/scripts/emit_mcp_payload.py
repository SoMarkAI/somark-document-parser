from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_payload(output_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8-sig")
    )
    title = manifest.get("source_title") or manifest.get("filename_fallback")
    if not isinstance(title, str):
        raise ValueError("manifest does not contain a string page title")
    content = (output_dir / "page.nfm.md").read_text(encoding="utf-8")
    merge_mappings = [
        mapping.get("merge_degradation", {})
        for mapping in manifest.get("mappings", [])
        if isinstance(mapping, dict)
        and isinstance(mapping.get("merge_degradation"), dict)
        and mapping["merge_degradation"].get("used")
    ]
    merged_region_count = sum(len(mapping.get("ranges", [])) for mapping in merge_mappings)
    return {
        "title": title,
        "content": content,
        "has_merged_cells": merged_region_count > 0,
        "merged_table_count": len(merge_mappings),
        "merged_region_count": merged_region_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit an ASCII-safe JSON payload for the Notion MCP call."
    )
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_payload(args.output_dir), ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
