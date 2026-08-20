"""Explicit source-artifact validation and route data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable


class RouteName(str, Enum):
    DOCUMENT = "document"
    SHEET = "sheet"
    AITABLE = "aitable"


@dataclass(frozen=True)
class SourceArtifacts:
    source_path: str | None
    source_hash: str
    markdown_path: str | None = None
    json_path: str | None = None
    assets_dir: str | None = None
    evidence_files: tuple[str, ...] = ()

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "markdown_path": self.markdown_path,
            "json_path": self.json_path,
            "assets_dir": self.assets_dir,
            "evidence_files": list(self.evidence_files),
        }


@dataclass(frozen=True)
class RouteTarget:
    route: RouteName
    title: str
    evidence_dir: str
    profile: str | None = None
    create_only: bool = True
    table_index: int | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("target title must not be empty")
        if not self.create_only:
            raise ValueError("the foundation supports create-only targets")
        if self.table_index is not None and self.table_index < 1:
            raise ValueError("table_index must be a positive integer")


@dataclass
class RouteResult:
    route: RouteName
    stage: str = "pending"
    target: dict[str, Any] = field(default_factory=dict)
    direct_url: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    statistics: dict[str, Any] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    readback: dict[str, Any] = field(default_factory=dict)
    evidence_files: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None

    @property
    def verified(self) -> bool:
        return self.stage == "verified" and self.error is None


def _existing_file(path: str | Path | None, label: str) -> Path | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} does not exist or is not a file: {resolved}")
    return resolved


def _existing_dir(path: str | Path | None, label: str) -> Path | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"{label} does not exist or is not a directory: {resolved}")
    return resolved


def _hash_files(paths: Iterable[Path]) -> str:
    inputs = sorted(paths, key=lambda item: str(item).casefold())
    digest = sha256()
    for path in inputs:
        if len(inputs) > 1:
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def resolve_explicit_artifacts(
    *,
    source_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    json_path: str | Path | None = None,
    assets_dir: str | Path | None = None,
) -> SourceArtifacts:
    """Validate explicitly supplied artifacts without discovering prior results."""

    source = _existing_file(source_path, "source")
    markdown = _existing_file(markdown_path, "Markdown artifact")
    json_file = _existing_file(json_path, "JSON artifact")
    assets = _existing_dir(assets_dir, "assets directory")

    if (markdown is None) != (json_file is None):
        raise ValueError("provide both explicit SoMark Markdown and JSON artifact paths")
    if markdown is None or json_file is None:
        if source is not None:
            raise ValueError(
                "a local source requires a fresh SoMark parse; pass both Markdown and JSON paths from that parse"
            )
        raise ValueError("provide a local source with fresh parse outputs or an explicit Markdown and JSON pair")

    evidence = tuple(str(path) for path in (source, markdown, json_file) if path is not None)
    hash_inputs = [source] if source is not None else [markdown, json_file]
    return SourceArtifacts(
        source_path=str(source) if source else None,
        source_hash=_hash_files(hash_inputs),
        markdown_path=str(markdown),
        json_path=str(json_file),
        assets_dir=str(assets) if assets else None,
        evidence_files=evidence,
    )
