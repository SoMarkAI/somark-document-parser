"""Shared foundation contracts for the SoMark-to-DingTalk skill."""

from .artifacts import RouteName, RouteResult, RouteTarget, SourceArtifacts, resolve_explicit_artifacts
from .dws_runner import DwsRunResult, DwsRunner
from .errors import ErrorKind, StructuredError, redact_sensitive
from .manifest import MANIFEST_SCHEMA_VERSION, ManifestStage, read_manifest, write_manifest_atomic
from .publish import publish, resume

__all__ = [
    "DwsRunResult",
    "DwsRunner",
    "ErrorKind",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestStage",
    "RouteName",
    "RouteResult",
    "RouteTarget",
    "SourceArtifacts",
    "StructuredError",
    "resolve_explicit_artifacts",
    "read_manifest",
    "redact_sensitive",
    "publish",
    "resume",
    "write_manifest_atomic",
]
