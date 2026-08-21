"""Minimal standard-library client for SoMark asynchronous document parsing."""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://somark.cn/api/v1"
MAX_FILE_BYTES = 200 * 1024 * 1024

DEFAULT_ELEMENT_FORMATS = {
    "image": "url",
    "formula": "latex",
    "table": "html",
    "cs": "image",
}

DEFAULT_FEATURE_CONFIG = {
    "enable_text_cross_page": False,
    "enable_table_cross_page": False,
    "enable_title_level_recognition": True,
    "enable_inline_image": False,
    "enable_table_image": False,
    "enable_image_understanding": False,
    "keep_header_footer": False,
}


class SoMarkError(RuntimeError):
    """Raised when SoMark cannot submit or complete a parse request."""


class SoMarkTransientError(SoMarkError):
    """Raised for a temporary network or server error that polling may retry."""


def _escape_disposition(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("multipart field values cannot contain line breaks")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _encode_multipart(
    fields: list[tuple[str, str]], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = f"----somark-outline-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    "Content-Disposition: form-data; "
                    f'name="{_escape_disposition(name)}"\r\n\r\n'
                ).encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                "Content-Disposition: form-data; "
                f'name="{_escape_disposition(file_field)}"; '
                f'filename="{_escape_disposition(file_path.name)}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _decode_json(raw: bytes, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoMarkError(f"{context} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SoMarkError(f"{context} returned a non-object JSON response")
    return payload


def _request_json(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        error_type = SoMarkTransientError if exc.code >= 500 else SoMarkError
        raise error_type(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SoMarkTransientError(f"network request failed: {exc.reason}") from exc
    return _decode_json(raw, "SoMark")


def _check_api_error(payload: dict[str, Any], context: str) -> None:
    code = payload.get("code")
    if code not in (None, 0, "0"):
        message = payload.get("message") or payload.get("msg") or payload
        raise SoMarkError(f"{context} failed with code {code}: {message}")


def submit_parse(
    file_path: Path,
    api_key: str,
    base_url: str,
    request_timeout: float = 120.0,
) -> str:
    if not file_path.is_file():
        raise FileNotFoundError(f"file not found: {file_path}")
    if file_path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file exceeds SoMark's 200 MB limit")

    fields = [
        ("api_key", api_key),
        ("output_formats", "json"),
        ("element_formats", json.dumps(DEFAULT_ELEMENT_FORMATS, ensure_ascii=False)),
        ("feature_config", json.dumps(DEFAULT_FEATURE_CONFIG, ensure_ascii=False)),
    ]
    body, content_type = _encode_multipart(fields, "file", file_path)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/parse/async",
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    payload = _request_json(request, request_timeout)
    _check_api_error(payload, "parse submission")
    data = payload.get("data")
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise SoMarkError(f"parse submission did not return task_id: {payload}")
    return task_id


def poll_parse(
    task_id: str,
    api_key: str,
    base_url: str,
    poll_interval: float = 2.0,
    overall_timeout: float = 1800.0,
    request_timeout: float = 120.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + overall_timeout
    last_error: str | None = None

    while time.monotonic() < deadline:
        time.sleep(max(0.1, poll_interval))
        form = urllib.parse.urlencode(
            {"api_key": api_key, "task_id": task_id}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/parse/async_check",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            payload = _request_json(request, request_timeout)
        except SoMarkTransientError as exc:
            last_error = str(exc)
            continue
        _check_api_error(payload, "parse polling")

        data = payload.get("data")
        status = str(data.get("status", "")).upper() if isinstance(data, dict) else ""
        if status == "SUCCESS":
            return payload
        if status in {"FAILED", "FAILURE", "ERROR"}:
            raise SoMarkError(f"SoMark parse failed: {data}")

    suffix = f"; last polling error: {last_error}" if last_error else ""
    raise SoMarkError(f"parse polling timed out for task_id={task_id}{suffix}")


def parse_document(
    file_path: Path,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    poll_interval: float = 2.0,
    overall_timeout: float = 1800.0,
) -> dict[str, Any]:
    resolved_key = api_key or os.environ.get("SOMARK_API_KEY", "")
    if not resolved_key:
        raise SoMarkError("SOMARK_API_KEY is not configured")
    resolved_url = (base_url or os.environ.get("SOMARK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if not resolved_url:
        raise SoMarkError("SoMark base URL is empty")

    task_id = submit_parse(file_path, resolved_key, resolved_url)
    return poll_parse(
        task_id,
        resolved_key,
        resolved_url,
        poll_interval=poll_interval,
        overall_timeout=overall_timeout,
    )
