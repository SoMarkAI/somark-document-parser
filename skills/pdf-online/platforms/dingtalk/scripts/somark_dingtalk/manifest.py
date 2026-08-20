"""Versioned, atomic and redacted route manifest persistence."""

from __future__ import annotations

from enum import Enum
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, MutableMapping

from .errors import redact_sensitive


MANIFEST_SCHEMA_VERSION = 1


class ManifestStage(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WRITTEN = "written"
    VERIFIED = "verified"
    FAILED = "failed"
    PARTIAL = "partial"


_REQUIRED_KEYS = {
    "schema_version", "route", "source", "source_hash", "somark_artifacts", "dws_cli_version",
    "target", "stage", "timings", "statistics", "degradations", "warnings", "ledger", "readback", "error",
}


def new_manifest(
    *,
    route: str,
    source: str | None,
    source_hash: str,
    somark_artifacts: Mapping[str, Any],
    dws_cli_version: str,
    target: Mapping[str, Any],
) -> dict[str, Any]:
    data = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "route": route,
        "source": source,
        "source_hash": source_hash,
        "somark_artifacts": dict(somark_artifacts),
        "dws_cli_version": dws_cli_version,
        "target": {**dict(target), "direct_url": target.get("direct_url")},
        "stage": ManifestStage.PENDING.value,
        "timings": {},
        "statistics": {},
        "degradations": [],
        "warnings": [],
        "ledger": [],
        "readback": {},
        "error": None,
    }
    validate_manifest(data)
    return data


def validate_manifest(data: Mapping[str, Any]) -> None:
    missing = sorted(_REQUIRED_KEYS.difference(data))
    if missing:
        raise ValueError("manifest is missing required fields: " + ", ".join(missing))
    if data["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema version: {data['schema_version']!r}")
    if data["route"] not in {"document", "sheet", "aitable"}:
        raise ValueError(f"invalid manifest route: {data['route']!r}")
    if data["stage"] not in {stage.value for stage in ManifestStage}:
        raise ValueError(f"invalid manifest stage: {data['stage']!r}")
    if not isinstance(data["source_hash"], str) or len(data["source_hash"]) != 64:
        raise ValueError("source_hash must be a SHA-256 hex digest")
    for key in ("somark_artifacts", "target", "timings", "statistics", "readback"):
        if not isinstance(data[key], Mapping):
            raise ValueError(f"manifest field {key!r} must be an object")
    for key in ("degradations", "warnings", "ledger"):
        if not isinstance(data[key], list):
            raise ValueError(f"manifest field {key!r} must be an array")


def set_stage(data: MutableMapping[str, Any], stage: ManifestStage, *, error: Any = None) -> None:
    data["stage"] = stage.value
    data["error"] = redact_sensitive(error)
    validate_manifest(data)


def write_manifest_atomic(path: str | Path, data: Mapping[str, Any]) -> Path:
    safe = redact_sensitive(dict(data))
    validate_manifest(safe)
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(safe, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def read_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")
    validate_manifest(data)
    return data
