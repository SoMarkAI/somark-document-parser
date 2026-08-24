"""Generate a nested catalog tree JSON from files or SoMark JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from somark_client import SoMarkError, parse_document


SUPPORTED_DOCUMENT_FORMATS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".jp2",
    ".dib",
    ".ppm",
    ".pgm",
    ".pbm",
    ".gif",
    ".heic",
    ".heif",
    ".webp",
    ".xpm",
    ".tga",
    ".dds",
    ".xbm",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xlsx",
    ".xlsm",
    ".xls",
}

class CatalogError(ValueError):
    """Raised when input JSON does not contain a usable SoMark catalog."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a nested catalog tree JSON with SoMark"
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", "-f", help="source document path")
    input_group.add_argument("--dir", "-d", help="directory of source documents")
    input_group.add_argument("--json", dest="json_input", help="SoMark JSON path")
    parser.add_argument(
        "--output",
        "-o",
        default="catalog-output",
        help="output directory (default: ./catalog-output)",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="override SOMARK_BASE_URL for source document parsing",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="poll interval in seconds (default: 2)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="overall polling timeout in seconds (default: 1800)",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise FileNotFoundError(f"JSON file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {path}: {exc}") from exc


def _catalog_value(container: Any) -> list[Any] | None:
    if not isinstance(container, dict) or "catalog" not in container:
        return None
    value = container.get("catalog")
    if value is None:
        return []
    if not isinstance(value, list):
        raise CatalogError("catalog exists but is not an array")
    return value


def extract_catalog(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise CatalogError("SoMark JSON must be an object or a catalog array")

    candidates: list[Any] = [payload]
    result = payload.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        data_result = data.get("result")
        if isinstance(data_result, dict):
            candidates.append(data_result)

    for candidate in candidates:
        catalog = _catalog_value(candidate)
        if catalog is not None:
            return catalog

    raise CatalogError(
        "no catalog found; expected data.result.catalog, result.catalog, "
        "top-level catalog, or a direct catalog array"
    )


def source_name_from_payload(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    candidates: list[Any] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        result = data.get("result")
        if isinstance(result, dict):
            candidates.append(result)
    result = payload.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    for candidate in candidates:
        value = candidate.get("file_name") if isinstance(candidate, dict) else None
        if isinstance(value, str) and value.strip():
            return Path(value.strip()).name
    return fallback


def safe_stem(value: str, fallback: str) -> str:
    stem = Path(value).stem.strip() or fallback
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    return stem.rstrip(". ") or fallback


def write_catalog(
    catalog: list[Any], output_dir: Path, output_stem: str
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / f"{output_stem}.catalog.json"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"catalog_json": str(catalog_path.resolve())}


def document_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"directory not found: {directory}")
    paths = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_FORMATS
        ),
        key=lambda path: path.name.lower(),
    )
    if not paths:
        supported = ", ".join(sorted(SUPPORTED_DOCUMENT_FORMATS))
        raise ValueError(f"no supported documents found; supported extensions: {supported}")
    return paths


def unique_stems(paths: list[Path]) -> dict[Path, str]:
    counts: dict[str, int] = {}
    result: dict[Path, str] = {}
    for path in paths:
        base = safe_stem(path.name, "document")
        key = base.casefold()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] == 1:
            result[path] = base
        else:
            result[path] = f"{base}-{path.suffix.lower().lstrip('.')}-{counts[key]}"
    return result


def process_document(
    path: Path,
    output_dir: Path,
    output_stem: str,
    args: argparse.Namespace,
) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"document not found: {path}")
    if path.suffix.lower() not in SUPPORTED_DOCUMENT_FORMATS:
        raise ValueError(f"unsupported document format: {path.suffix}")
    print(f"Parsing with SoMark: {path}")
    response = parse_document(
        path,
        base_url=args.base_url or None,
        poll_interval=args.poll_interval,
        overall_timeout=args.timeout,
    )
    catalog = extract_catalog(response)
    return write_catalog(catalog, output_dir, output_stem)


def process_json(path: Path, output_dir: Path) -> dict[str, str]:
    payload = load_json(path)
    catalog = extract_catalog(payload)
    source_name = source_name_from_payload(payload, path.name)
    output_stem = safe_stem(source_name, safe_stem(path.name, "document"))
    return write_catalog(catalog, output_dir, output_stem)


def print_outputs(outputs: dict[str, str]) -> None:
    for label, path in outputs.items():
        print(f"{label}: {path}")


def main() -> int:
    args = parse_args()
    if args.poll_interval <= 0:
        print("error: --poll-interval must be greater than zero", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("error: --timeout must be greater than zero", file=sys.stderr)
        return 2

    output_dir = Path(args.output).expanduser().resolve()
    try:
        if args.json_input:
            outputs = process_json(
                Path(args.json_input).expanduser().resolve(), output_dir
            )
            print_outputs(outputs)
            return 0

        if args.file:
            path = Path(args.file).expanduser().resolve()
            output_stem = safe_stem(path.name, "document")
            outputs = process_document(path, output_dir, output_stem, args)
            print_outputs(outputs)
            return 0

        paths = document_paths(Path(args.dir).expanduser().resolve())
        stems = unique_stems(paths)
        failures = 0
        for path in paths:
            try:
                outputs = process_document(path, output_dir, stems[path], args)
                print_outputs(outputs)
            except Exception as exc:  # Continue other documents in directory mode.
                failures += 1
                print(f"failed: {path}: {exc}", file=sys.stderr)
        print(f"Completed {len(paths) - failures}/{len(paths)} documents")
        return 1 if failures else 0
    except (CatalogError, SoMarkError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
