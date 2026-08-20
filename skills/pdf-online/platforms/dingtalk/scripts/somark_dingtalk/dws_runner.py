"""Safe, deterministic subprocess runner for DWS JSON commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shutil
import subprocess
from time import monotonic
from typing import Any, Callable, Sequence

from .errors import ErrorKind, StructuredError, normalize_dws_error, redact_command, redact_sensitive


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")


@dataclass(frozen=True)
class DwsRunResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: Any
    stderr: str
    duration_seconds: float
    error: StructuredError | None = None
    call_sequence: int | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    json_values: tuple[Any, ...] = ()
    json_source: str | None = None

    @property
    def command_succeeded(self) -> bool:
        return self.exit_code == 0 and self.error is None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": redact_sensitive(self.stdout),
            "stderr": redact_sensitive(self.stderr),
            "duration_seconds": self.duration_seconds,
            "error": self.error.to_safe_dict() if self.error else None,
            "call_sequence": self.call_sequence,
            "raw_stdout": redact_sensitive(self.raw_stdout),
            "raw_stderr": redact_sensitive(self.raw_stderr),
            "json_values": redact_sensitive(list(self.json_values)),
            "json_source": self.json_source,
        }


def parse_json_values(text: str) -> tuple[Any, ...]:
    """Return top-level JSON values embedded in DWS progress or NDJSON text."""

    clean = _ANSI_RE.sub("", text).lstrip("\ufeff")
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while index < len(clean):
        char = clean[index]
        if char not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(clean, index)
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(value)
        index = end
    return tuple(values)


def parse_progress_json(text: str) -> Any:
    """Parse the last complete JSON value from progress, NDJSON, or mixed output."""

    values = parse_json_values(text)
    if not values:
        raise ValueError("DWS output did not contain a JSON object or array")
    return values[-1]


class DwsRunner:
    def __init__(
        self,
        *,
        expected_version: str = "1.0.57",
        which: Callable[[str], str | None] = shutil.which,
        process_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.expected_version = expected_version
        self._which = which
        self._process_run = process_run
        self._call_sequence = 0

    def resolve_executable(self) -> str:
        executable = self._which("dws")
        if not executable:
            raise FileNotFoundError("dws was not found on PATH")
        return executable

    @staticmethod
    def _json_args(arguments: Sequence[str]) -> list[str]:
        args = [str(item) for item in arguments]
        for index, item in enumerate(args):
            if item in {"--format", "-f"}:
                if index + 1 >= len(args) or args[index + 1].casefold() != "json":
                    raise ValueError("DWS foundation commands require JSON output")
                return args
            if item.startswith("--format="):
                if item.split("=", 1)[1].casefold() != "json":
                    raise ValueError("DWS foundation commands require JSON output")
                return args
            if item.startswith("-f="):
                if item.split("=", 1)[1].casefold() != "json":
                    raise ValueError("DWS foundation commands require JSON output")
                return args
        args.extend(("--format", "json"))
        return args

    def run_json(
        self,
        arguments: Sequence[str],
        *,
        profile: str | None = None,
        timeout_seconds: float = 60.0,
        dry_run: bool = False,
        stdin_text: str | None = None,
    ) -> DwsRunResult:
        self._call_sequence += 1
        call_sequence = self._call_sequence
        if isinstance(arguments, (str, bytes)):
            raise TypeError("arguments must be a sequence of strings, not a shell command")
        args = self._json_args(arguments)
        if any(item in {"--yes", "-y"} or item.startswith("--yes=") or item.startswith("-y=") for item in args):
            raise ValueError("the shared runner does not suppress DWS confirmation")
        if profile is not None:
            if not profile.strip():
                raise ValueError("profile must not be blank")
            if any(item == "--profile" or item.startswith("--profile=") for item in args):
                raise ValueError("pass profile through the dedicated profile parameter")
            args.extend(("--profile", profile))
        if dry_run and "--dry-run" not in args:
            args.append("--dry-run")

        try:
            executable = self.resolve_executable()
        except FileNotFoundError as exc:
            error = StructuredError(ErrorKind.DWS_NOT_INSTALLED, str(exc))
            return DwsRunResult(
                ("dws", *redact_command(args)),
                None,
                None,
                "",
                0.0,
                error,
                call_sequence=call_sequence,
            )

        command = [executable, *args]
        safe_command = redact_command(command)
        started = monotonic()
        try:
            completed = self._process_run(
                command,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = monotonic() - started
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            error = StructuredError(ErrorKind.TIMEOUT, f"DWS command timed out after {timeout_seconds:g} seconds")
            safe_stderr = str(redact_sensitive(stderr))
            return DwsRunResult(
                safe_command,
                None,
                None,
                safe_stderr,
                duration,
                error,
                call_sequence=call_sequence,
                raw_stderr=safe_stderr,
            )

        duration = monotonic() - started
        raw_stdout = str(redact_sensitive(completed.stdout or ""))
        raw_stderr = str(redact_sensitive(completed.stderr or ""))
        stdout_values = parse_json_values(completed.stdout or "")
        stderr_values = parse_json_values(completed.stderr or "")
        stdout = stdout_values[-1] if stdout_values else None
        stderr_payload = stderr_values[-1] if stderr_values else None
        stderr_error = normalize_dws_error(stderr_payload)
        json_source: str | None = None
        if completed.returncode != 0 and stderr_error is not None:
            stdout = stderr_payload
            json_source = "stderr"
        elif stdout_values:
            json_source = "stdout"
        elif stderr_values:
            stdout = stderr_payload
            json_source = "stderr"

        if stdout is None:
            kind = ErrorKind.INVALID_JSON if completed.returncode == 0 else ErrorKind.PROCESS_FAILURE
            error = StructuredError(
                kind,
                "DWS output did not contain a JSON object or array",
                details={
                    "exit_code": completed.returncode,
                    "stdout_empty": not bool((completed.stdout or "").strip()),
                    "stderr_empty": not bool((completed.stderr or "").strip()),
                },
            )
            return DwsRunResult(
                safe_command,
                completed.returncode,
                None,
                raw_stderr,
                duration,
                error,
                call_sequence=call_sequence,
                raw_stdout=raw_stdout,
                raw_stderr=raw_stderr,
                json_values=(*stdout_values, *stderr_values),
                json_source=None,
            )

        normalized = normalize_dws_error(stdout)
        if normalized is None and completed.returncode != 0:
            normalized = StructuredError(
                ErrorKind.PROCESS_FAILURE,
                "DWS command returned a non-zero exit code",
                details={"exit_code": completed.returncode, "stdout": redact_sensitive(stdout)},
            )
        return DwsRunResult(
            safe_command,
            completed.returncode,
            redact_sensitive(stdout),
            raw_stderr,
            duration,
            normalized,
            call_sequence=call_sequence,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            json_values=tuple(redact_sensitive((*stdout_values, *stderr_values))),
            json_source=json_source,
        )

    def read_version(self, timeout_seconds: float = 15.0) -> tuple[str | None, StructuredError | None]:
        try:
            executable = self.resolve_executable()
        except FileNotFoundError as exc:
            return None, StructuredError(ErrorKind.DWS_NOT_INSTALLED, str(exc))
        try:
            completed = self._process_run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, StructuredError(ErrorKind.TIMEOUT, "DWS version check timed out")
        match = _VERSION_RE.search((completed.stdout or "") + "\n" + (completed.stderr or ""))
        if completed.returncode != 0 or not match:
            return None, StructuredError(ErrorKind.PROCESS_FAILURE, "unable to read the DWS version")
        version = match.group(1)
        if version != self.expected_version:
            return version, StructuredError(
                ErrorKind.VERSION_MISMATCH,
                f"DWS v{self.expected_version} is required; found v{version}",
            )
        return version, None
