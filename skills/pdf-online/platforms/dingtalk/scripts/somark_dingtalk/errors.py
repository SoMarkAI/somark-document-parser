"""DWS error normalization and credential-safe serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit


REDACTED = "[REDACTED]"


class ErrorKind(str, Enum):
    DWS_NOT_INSTALLED = "dws_not_installed"
    VERSION_MISMATCH = "version_mismatch"
    PROFILE_REQUIRED = "profile_required"
    PROFILE_AMBIGUOUS = "profile_ambiguous"
    CONFIRMATION_REQUIRED = "confirmation_required"
    AUTHENTICATION_PERMISSION = "authentication_or_permission"
    INVALID_ARGUMENT = "invalid_argument"
    RETRYABLE_SERVICE = "retryable_service"
    BUSINESS_VALIDATION = "business_validation"
    INVALID_JSON = "invalid_json"
    TIMEOUT = "timeout"
    PROCESS_FAILURE = "process_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StructuredError:
    kind: ErrorKind
    message: str
    code: str | int | None = None
    reason: str | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = None
    next_retry_at: str | None = None
    hint: str | None = None
    actions: tuple[Any, ...] = ()
    details: Any = None

    @property
    def may_retry_once(self) -> bool:
        return self.retryable is True

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["actions"] = list(self.actions)
        return redact_sensitive(data)


_SENSITIVE_KEYS = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey", "token",
    "accesstoken", "refreshtoken", "filetoken", "uploadtoken", "clientsecret", "secret",
    "password", "passwd", "credential", "credentials", "privatekey", "signature",
}
_SIGNED_QUERY_KEYS = {"signature", "expires", "credential", "securitytoken", "accesskeyid", "ossaccesskeyid"}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_COOKIE_RE = re.compile(r"(?im)\b(set-cookie|cookie)\s*:\s*[^\r\n]+")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|password)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    keys = {_normalized_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    signed = any(key in _SIGNED_QUERY_KEYS or key.startswith("xoss") or key.startswith("xacs") for key in keys)
    if not signed:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "REDACTED", parts.fragment))


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    value = _COOKIE_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    value = _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", value)
    return _URL_RE.sub(lambda match: _redact_url(match.group(0)), value)


def redact_sensitive(value: Any) -> Any:
    """Recursively redact credentials while preserving ordinary business IDs."""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _normalized_key(key) in _SENSITIVE_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def redact_command(command: Sequence[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    hide_next = False
    sensitive_flags = {
        "--token", "--access-token", "--refresh-token", "--client-secret", "--password", "--profile",
    }
    for part in command:
        if hide_next:
            redacted.append(REDACTED)
            hide_next = False
            continue
        if part.casefold() in sensitive_flags:
            redacted.append(part)
            hide_next = True
            continue
        if "=" in part and part.split("=", 1)[0].casefold() in sensitive_flags:
            redacted.append(part.split("=", 1)[0] + "=" + REDACTED)
            continue
        redacted.append(_redact_text(part))
    return tuple(redacted)


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _error_mapping(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    if isinstance(payload.get("error"), Mapping):
        return payload["error"]
    data = payload.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("error"), Mapping):
        return data["error"]
    if payload.get("success") is False or payload.get("ok") is False:
        return payload
    return None


def normalize_dws_error(payload: Any, fallback_message: str = "DWS command failed") -> StructuredError | None:
    raw = _error_mapping(payload)
    if raw is None:
        return None
    code = _first(raw, "code", "errorCode", "error_code")
    reason = _first(raw, "reason", "errorReason", "error_reason")
    message = str(_first(raw, "message", "msg", "errorMessage") or fallback_message)
    retryable_value = _first(raw, "retryable", "isRetryable", "is_retryable")
    retryable = retryable_value if isinstance(retryable_value, bool) else None
    retry_after = _first(raw, "retryAfterSeconds", "retry_after_seconds", "retryAfter")
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    next_retry = _first(raw, "nextRetryAt", "next_retry_at")
    hint = _first(raw, "hint", "suggestion")
    actions_value = _first(raw, "actions", "action")
    if isinstance(actions_value, list):
        actions = tuple(actions_value)
    elif actions_value is None:
        actions = ()
    else:
        actions = (actions_value,)

    combined = " ".join(str(value) for value in (code, reason, message) if value is not None).casefold()
    if "confirmation" in combined or "confirm" in combined:
        kind = ErrorKind.CONFIRMATION_REQUIRED
    elif any(term in combined for term in ("unauthorized", "forbidden", "permission", "authentication", "login")):
        kind = ErrorKind.AUTHENTICATION_PERMISSION
    elif any(term in combined for term in ("profile required", "no current profile", "profile missing")):
        kind = ErrorKind.PROFILE_REQUIRED
    elif "profile" in combined and "ambiguous" in combined:
        kind = ErrorKind.PROFILE_AMBIGUOUS
    elif retryable is True:
        kind = ErrorKind.RETRYABLE_SERVICE
    elif any(term in combined for term in ("invalid argument", "invalid parameter", "bad request")):
        kind = ErrorKind.INVALID_ARGUMENT
    elif any(
        term in combined
        for term in (
            "validation",
            "readback",
            "mismatch",
            "business_error",
            "jsonmltonode",
            "jsonml",
        )
    ):
        kind = ErrorKind.BUSINESS_VALIDATION
    else:
        kind = ErrorKind.UNKNOWN

    return StructuredError(
        kind=kind,
        message=message,
        code=code,
        reason=str(reason) if reason is not None else None,
        retryable=retryable,
        retry_after_seconds=retry_after,
        next_retry_at=str(next_retry) if next_retry is not None else None,
        hint=str(hint) if hint is not None else None,
        actions=actions,
        details=redact_sensitive(raw),
    )
