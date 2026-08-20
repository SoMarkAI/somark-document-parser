#!/usr/bin/env python3
"""Deterministically select the somark-to-notion input mode without filesystem search."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


RAW_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".heic", ".heif",
    ".doc", ".docx", ".ppt", ".pptx",
}


@dataclass(frozen=True)
class Route:
    target: str
    mode: str
    call_somark: bool
    somark_call_limit: int
    json_path: str | None
    markdown_path: str | None
    degradation: str | None


def route_input(
    inputs: list[Path], *, use_existing_results: bool = False,
    somark_calls_already_made: int = 0, database_requested: bool = False,
    existing_database_supplied: bool = False,
) -> Route:
    """Route only explicit inputs; never search for related or historical files."""
    target = "database" if database_requested else "page"
    if existing_database_supplied:
        if not database_requested:
            raise ValueError("an existing database target requires explicit database intent")
        raise NotImplementedError(
            "writing into an existing Notion database is not implemented; create a new database instead"
        )
    json_paths = [path for path in inputs if path.suffix.lower() == ".json"]
    markdown_paths = [path for path in inputs if path.suffix.lower() in {".md", ".markdown"}]
    raw_paths = [path for path in inputs if path.suffix.lower() in RAW_EXTENSIONS]

    if len(json_paths) > 1 or len(markdown_paths) > 1 or len(raw_paths) > 1:
        raise ValueError("the minimum skill accepts one document/result set per task")
    if raw_paths and (json_paths or markdown_paths):
        raise ValueError(
            "do not mix a raw document with explicit results; provide the raw file for a fresh parse or the exact Markdown-and-JSON pair"
        )
    if json_paths or use_existing_results:
        if not json_paths:
            raise ValueError(
                "existing-results mode requires the exact SoMark Markdown-and-JSON pair"
            )
        if not markdown_paths:
            raise ValueError(
                "existing-results mode requires the exact SoMark Markdown-and-JSON pair"
            )
        return Route(
            target, "existing_results", False, 0, str(json_paths[0]),
            str(markdown_paths[0]), None,
        )
    if raw_paths:
        if somark_calls_already_made >= 1:
            raise ValueError("SoMark was already called once in this task; reuse that result")
        return Route(target, "raw_file", True, 1, None, None, None)
    if markdown_paths:
        raise ValueError(
            "Markdown-only input is unsupported; provide the exact SoMark Markdown-and-JSON pair"
        )
    raise ValueError("provide one raw document or an exact SoMark Markdown-and-JSON pair")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--use-existing-results", action="store_true")
    parser.add_argument("--somark-calls-already-made", type=int, default=0)
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--existing-database-supplied", action="store_true")
    args = parser.parse_args()
    route = route_input(
        args.inputs,
        use_existing_results=args.use_existing_results,
        somark_calls_already_made=args.somark_calls_already_made,
        database_requested=args.database,
        existing_database_supplied=args.existing_database_supplied,
    )
    print(json.dumps(asdict(route), ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
