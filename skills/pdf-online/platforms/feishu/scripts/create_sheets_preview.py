#!/usr/bin/env python
"""Create a content-only Feishu workbook and return a resumable preview checkpoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def find_lark_cli() -> str:
    for candidate in ("lark-cli.cmd", "lark-cli"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("lark-cli is not available on PATH")


def _json_response(output: str) -> Mapping[str, Any]:
    stripped = output.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, Mapping):
        return value

    decoder = json.JSONDecoder()
    responses: list[Mapping[str, Any]] = []
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and not output[index + end :].strip():
            responses.append(value)
    if not responses:
        raise RuntimeError("lark-cli did not return a JSON response")
    return responses[-1]


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def create_preview(
    payload_dir: str | Path,
    *,
    title: str,
    checkpoint_path: str | Path | None = None,
    cli: str | None = None,
    runner: ProcessRunner | None = None,
) -> dict[str, Any]:
    """Create only the base workbook; styles, merges, and images remain deferred."""

    payload = Path(payload_dir).expanduser().resolve()
    sheets_path = payload / "sheets_payload.json"
    if not sheets_path.is_file():
        raise FileNotFoundError(f"sheets_payload.json was not found: {sheets_path}")
    if not title.strip():
        raise ValueError("title must not be empty")

    active_cli = cli or find_lark_cli()
    active_runner = runner or subprocess.run
    command: Sequence[str] = (
        active_cli,
        "sheets",
        "+workbook-create",
        "--title",
        title,
        "--sheets",
        "@sheets_payload.json",
        "--as",
        "user",
    )
    completed = active_runner(
        list(command),
        cwd=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    response = _json_response(completed.stdout or "")
    data = response.get("data")
    spreadsheet = data.get("spreadsheet") if isinstance(data, Mapping) else None
    url = spreadsheet.get("url") if isinstance(spreadsheet, Mapping) else None
    token = spreadsheet.get("spreadsheet_token") if isinstance(spreadsheet, Mapping) else None
    if completed.returncode != 0 or response.get("ok") is not True or not url or not token:
        detail = completed.stderr.strip() or str(response.get("data") or response)
        raise RuntimeError(f"content-only workbook creation failed: {detail}")

    checkpoint = {
        "schema_version": 1,
        "phase": "preview_ready",
        "preview_url": str(url),
        "spreadsheet_token": str(token),
        "postprocess_pending": True,
        "payload_dir": str(payload),
        "title": title,
    }
    destination = (
        Path(checkpoint_path).expanduser().resolve()
        if checkpoint_path is not None
        else payload / "preview_checkpoint.json"
    )
    _write_checkpoint(destination, checkpoint)
    return {**checkpoint, "checkpoint": str(destination)}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser()
    parser.add_argument("payload_dir", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = create_preview(
            args.payload_dir,
            title=args.title,
            checkpoint_path=args.output,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "phase": "preview_failed", "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
