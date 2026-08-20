#!/usr/bin/env python3
"""Apply a prepared SoMark Sheets payload with rate-limit-aware batching."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


RETRYABLE_MARKERS = (
    "99991400",
    "frequency limit",
    "rate limit",
    "too many requests",
)

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
}
IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
MAX_IMAGE_BYTES = 50 * 1024 * 1024
CELL_RANGE_RE = re.compile(r"^([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)$")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def download_remote_image(
    reference: str,
    destination_dir: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> Path:
    """Download one SoMark image URL for the post-preview image stage."""

    parsed = urlparse(reference.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("image reference is not an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("image URL must not contain credentials")

    request = Request(reference, headers={"User-Agent": "SoMark-Feishu-Adapter/1"})
    destination_dir.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type", ""))
        content_type = content_type.split(";", 1)[0].strip().casefold()
        suffix = Path(parsed.path).suffix.casefold()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = IMAGE_CONTENT_TYPE_EXTENSIONS.get(content_type, "")
        if not suffix or (
            content_type and not content_type.startswith("image/")
            and Path(parsed.path).suffix.casefold() not in IMAGE_EXTENSIONS
        ):
            raise ValueError(f"remote resource is not a supported image: {content_type or 'unknown'}")

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("remote image exceeds the download size limit")
        body = bytearray()
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("remote image exceeds the download size limit")

    digest = sha256(reference.encode("utf-8")).hexdigest()[:20]
    destination = destination_dir / f"remote_{digest}{suffix}"
    destination.write_bytes(body)
    return destination


def resolve_image_path(
    image: dict[str, Any],
    staging_dir: Path,
    cache: dict[str, Path],
) -> Path | None:
    local_path = image.get("local_path")
    if local_path and Path(local_path).is_file():
        return Path(local_path).resolve()
    for reference in image.get("urls") or []:
        reference = str(reference).strip()
        if reference in cache:
            return cache[reference]
        try:
            downloaded = download_remote_image(reference, staging_dir)
        except (OSError, ValueError) as exc:
            print(
                f"Skipped table image download for {reference[:160]}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        cache[reference] = downloaded
        return downloaded
    return None


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def find_lark_cli() -> str:
    for candidate in ("lark-cli.cmd", "lark-cli"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("lark-cli is not available on PATH")


def run_lark(
    cli: str,
    args: list[str],
    *,
    cwd: Path,
    max_retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    attempt = 0
    while True:
        completed = subprocess.run(
            [cli, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = completed.stdout.strip()
        error_output = completed.stderr.strip()
        combined = f"{output}\n{error_output}".lower()
        try:
            response = json.loads(output) if output else {}
        except json.JSONDecodeError:
            response = {}

        if completed.returncode == 0 and response.get("ok") is True:
            return response

        retryable = any(marker in combined for marker in RETRYABLE_MARKERS)
        if not retryable or attempt >= max_retries:
            detail = output or error_output or f"exit code {completed.returncode}"
            raise RuntimeError(detail)

        attempt += 1
        wait_seconds = retry_delay * (2 ** (attempt - 1))
        print(
            f"Rate limit detected; retrying in {wait_seconds:.1f}s "
            f"({attempt}/{max_retries}).",
            flush=True,
        )
        time.sleep(wait_seconds)


def locator_args(args: argparse.Namespace) -> list[str]:
    if args.spreadsheet_token:
        return ["--spreadsheet-token", args.spreadsheet_token]
    if args.preview_checkpoint:
        checkpoint = load_json(args.preview_checkpoint.resolve())
        if checkpoint.get("phase") != "preview_ready":
            raise ValueError("preview checkpoint is not ready for enhancement")
        spreadsheet_token = checkpoint.get("spreadsheet_token")
        if not isinstance(spreadsheet_token, str) or not spreadsheet_token.strip():
            raise ValueError("preview checkpoint does not contain a spreadsheet token")
        return ["--spreadsheet-token", spreadsheet_token]
    return ["--url", args.url]


def build_merge_operations(
    styles: list[dict[str, Any]], *, reset_merges: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reset_operations: list[dict[str, Any]] = []
    merge_operations: list[dict[str, Any]] = []
    for style in styles:
        sheet_name = style["name"]
        cell_styles = style.get("cell_styles") or []
        if reset_merges and cell_styles:
            reset_operations.append(
                {
                    "shortcut": "+cells-unmerge",
                    "input": {
                        "sheet_name": sheet_name,
                        "range": cell_styles[0]["range"],
                    },
                }
            )
        for merge in style.get("cell_merges") or []:
            merge_input = {
                "sheet_name": sheet_name,
                "range": merge["range"],
            }
            if merge.get("merge_type"):
                merge_input["merge_type"] = merge["merge_type"]
            merge_operations.append(
                {"shortcut": "+cells-merge", "input": merge_input}
            )
    return reset_operations, merge_operations


def _column_index(name: str) -> int:
    value = 0
    for character in name:
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _merge_rectangle(value: str) -> tuple[int, int, int, int] | None:
    match = CELL_RANGE_RE.fullmatch(value.strip().upper())
    if match is None:
        return None
    start_column = _column_index(match.group(1))
    start_row = int(match.group(2)) - 1
    end_column = _column_index(match.group(3))
    end_row = int(match.group(4)) - 1
    if end_row < start_row or end_column < start_column:
        return None
    return start_row, start_column, end_row, end_column


def _rectangles_overlap(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return not (
        left[2] < right[0]
        or right[2] < left[0]
        or left[3] < right[1]
        or right[3] < left[1]
    )


def filter_conflicting_merge_operations(
    operations: list[dict[str, Any]],
    sheets: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep safe merges and degrade only ranges that would overlap or lose data."""

    sheet_data = {
        str(sheet.get("name")): sheet.get("data") or [] for sheet in (sheets or [])
    }
    accepted_rectangles: dict[str, list[tuple[int, int, int, int]]] = {}
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for operation in operations:
        merge_input = operation.get("input") or {}
        sheet_name = str(merge_input.get("sheet_name") or "")
        range_value = str(merge_input.get("range") or "")
        rectangle = _merge_rectangle(range_value)
        reason: str | None = None
        if rectangle is None:
            reason = "invalid_merge_range"
        elif any(
            _rectangles_overlap(rectangle, previous)
            for previous in accepted_rectangles.get(sheet_name, [])
        ):
            reason = "overlaps_an_earlier_merge"
        else:
            rows = sheet_data.get(sheet_name, [])
            start_row, start_column, end_row, end_column = rectangle
            for row_index in range(start_row, end_row + 1):
                for column_index in range(start_column, end_column + 1):
                    if row_index == start_row and column_index == start_column:
                        continue
                    value = (
                        rows[row_index][column_index]
                        if row_index < len(rows)
                        and column_index < len(rows[row_index])
                        else None
                    )
                    if value is not None and str(value).strip():
                        reason = "would_discard_non_anchor_value"
                        break
                if reason is not None:
                    break

        if reason is not None:
            skipped.append(
                {
                    "sheet_name": sheet_name,
                    "range": range_value,
                    "reason": reason,
                }
            )
            continue
        accepted.append(operation)
        accepted_rectangles.setdefault(sheet_name, []).append(rectangle)

    return accepted, skipped


def build_style_operations(styles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for style in styles:
        sheet_name = style["name"]
        for cell_style in style.get("cell_styles") or []:
            operations.append(
                {
                    "shortcut": "+cells-set-style",
                    "input": {"sheet_name": sheet_name, **cell_style},
                }
            )
        for row_size in style.get("row_sizes") or []:
            row_input = {
                "sheet_name": sheet_name,
                "range": row_size["range"],
            }
            if row_size.get("type") == "pixel":
                row_input["height"] = row_size["size"]
            else:
                row_input["type"] = row_size["type"]
            operations.append(
                {"shortcut": "+rows-resize", "input": row_input}
            )
        for col_size in style.get("col_sizes") or []:
            col_input = {
                "sheet_name": sheet_name,
                "range": col_size["range"],
            }
            if col_size.get("type") == "pixel":
                col_input["width"] = col_size["size"]
            else:
                col_input["type"] = col_size["type"]
            operations.append(
                {"shortcut": "+cols-resize", "input": col_input}
            )
    return operations


def apply_operation_batches(
    cli: str,
    operations: list[dict[str, Any]],
    *,
    prefix: str,
    temp_dir: Path,
    locator: list[str],
    batch_size: int,
    max_retries: int,
    retry_delay: float,
    inter_batch_delay: float,
) -> None:
    operation_batches = chunks(operations, batch_size)
    for index, operation_batch in enumerate(operation_batches, start=1):
        filename = f"{prefix}_{index:02d}.json"
        write_json(temp_dir / filename, operation_batch)
        run_lark(
            cli,
            [
                "sheets",
                "+batch-update",
                *locator,
                "--operations",
                f"@{temp_dir.name}/{filename}",
                "--yes",
                "--as",
                "user",
            ],
            cwd=temp_dir.parent,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        print(
            f"Applied {prefix} batch {index}/{len(operation_batches)} "
            f"({len(operation_batch)} operations).",
            flush=True,
        )
        if index < len(operation_batches):
            time.sleep(inter_batch_delay)


def apply_embedded_images(
    cli: str,
    manifest: dict[str, Any],
    *,
    locator: list[str],
    cwd: Path,
    max_retries: int,
    retry_delay: float,
    inter_image_delay: float,
) -> int:
    applied = 0
    cwd = cwd.resolve()
    with tempfile.TemporaryDirectory(prefix=".sheets_images_", dir=cwd) as raw:
        staging_dir = Path(raw)
        remote_cache: dict[str, Path] = {}
        for table in manifest.get("tables", []):
            sheet_name = table.get("sheet_name")
            for image in table.get("images", []):
                cell = image.get("cell")
                if not sheet_name or not cell:
                    continue
                image_path = resolve_image_path(image, staging_dir, remote_cache)
                if image_path is None:
                    continue
                try:
                    image_arg = image_path.relative_to(cwd)
                except ValueError:
                    staged_path = staging_dir / f"{applied + 1:04d}_{image_path.name}"
                    shutil.copy2(image_path, staged_path)
                    image_arg = staged_path.relative_to(cwd)
                run_lark(
                    cli,
                    [
                        "sheets",
                        "+cells-set-image",
                        *locator,
                        "--sheet-name",
                        str(sheet_name),
                        "--range",
                        str(cell),
                        "--image",
                        str(image_arg),
                        "--format",
                        "json",
                        "--as",
                        "user",
                    ],
                    cwd=cwd,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                )
                applied += 1
                time.sleep(inter_image_delay)
    return applied


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("payload_dir", type=Path)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--spreadsheet-token")
    target.add_argument("--url")
    target.add_argument(
        "--preview-checkpoint",
        type=Path,
        help="Resume enhancement on the content-only workbook created by create_sheets_preview.py.",
    )
    parser.add_argument("--reset-merges", action="store_true")
    parser.add_argument("--skip-merges", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--enhance-only",
        action="store_true",
        help=(
            "Apply styles, row/column sizes, merges, and images without "
            "rewriting worksheet values."
        ),
    )
    mode.add_argument(
        "--images-only",
        action="store_true",
        help="Only embed locally matched images into an existing workbook.",
    )
    parser.add_argument("--operation-batch-size", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=3.0)
    parser.add_argument("--inter-batch-delay", type=float, default=1.5)
    args = parser.parse_args()

    payload_dir = args.payload_dir.resolve()
    sheets = load_json(payload_dir / "sheets_payload.json")["sheets"]
    styles = load_json(payload_dir / "styles_payload.json")["styles"]
    manifest = load_json(payload_dir / "manifest.json")
    style_by_name = {style["name"]: style for style in styles}

    if len(sheets) != len(styles):
        raise ValueError("sheets_payload.json and styles_payload.json do not align")

    cli = find_lark_cli()
    locator = locator_args(args)

    if args.images_only:
        image_count = apply_embedded_images(
            cli,
            manifest,
            locator=locator,
            cwd=payload_dir,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
            inter_image_delay=args.inter_batch_delay,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "images-only",
                    "embedded_image_count": image_count,
                },
                ensure_ascii=False,
            )
        )
        return 0

    with tempfile.TemporaryDirectory(prefix=".sheets_deploy_", dir=payload_dir) as raw:
        temp_dir = Path(raw)
        if args.enhance_only:
            style_operations = build_style_operations(styles)
            if style_operations:
                apply_operation_batches(
                    cli,
                    style_operations,
                    prefix="styles",
                    temp_dir=temp_dir,
                    locator=locator,
                    batch_size=args.operation_batch_size,
                    max_retries=args.max_retries,
                    retry_delay=args.retry_delay,
                    inter_batch_delay=args.inter_batch_delay,
                )
        else:
            style_operations = []
            for index, sheet in enumerate(sheets, start=1):
                name = sheet["name"]
                style = dict(style_by_name[name])
                style.pop("cell_merges", None)
                sheet_file = f"sheet_{index:02d}.json"
                style_file = f"style_{index:02d}.json"
                write_json(temp_dir / sheet_file, {"sheets": [sheet]})
                write_json(temp_dir / style_file, {"styles": [style]})

                run_lark(
                    cli,
                    [
                        "sheets",
                        "+table-put",
                        *locator,
                        "--sheets",
                        f"@{temp_dir.name}/{sheet_file}",
                        "--styles",
                        f"@{temp_dir.name}/{style_file}",
                        "--as",
                        "user",
                    ],
                    cwd=payload_dir,
                    max_retries=args.max_retries,
                    retry_delay=args.retry_delay,
                )
                print(f"Wrote sheet {index}/{len(sheets)}: {name}", flush=True)
                if index < len(sheets):
                    time.sleep(args.inter_batch_delay)

        reset_operations, merge_operations = build_merge_operations(
            styles, reset_merges=args.reset_merges
        )
        requested_merge_count = len(merge_operations)
        merge_operations, skipped_merges = filter_conflicting_merge_operations(
            merge_operations, sheets
        )
        if reset_operations and not args.skip_merges:
            apply_operation_batches(
                cli,
                reset_operations,
                prefix="reset_merges",
                temp_dir=temp_dir,
                locator=locator,
                batch_size=args.operation_batch_size,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
                inter_batch_delay=args.inter_batch_delay,
            )
        if merge_operations and not args.skip_merges:
            apply_operation_batches(
                cli,
                merge_operations,
                prefix="merges",
                temp_dir=temp_dir,
                locator=locator,
                batch_size=args.operation_batch_size,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
                inter_batch_delay=args.inter_batch_delay,
            )
        image_count = apply_embedded_images(
            cli,
            manifest,
            locator=locator,
            cwd=payload_dir,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
            inter_image_delay=args.inter_batch_delay,
        )
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "enhance-only" if args.enhance_only else "full",
                "sheet_count": len(sheets),
                "style_operation_count": len(style_operations),
                "requested_merge_count": requested_merge_count,
                "merge_count": len(merge_operations),
                "skipped_merge_count": len(skipped_merges),
                "merge_degradations": skipped_merges,
                "embedded_image_count": image_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
