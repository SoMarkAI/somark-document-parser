# Common foundation contract

## Scope

Keep the shared layer limited to explicit source-artifact validation, typed routing data, safe DWS process execution, redaction, error normalization, and manifest persistence. Do not place document, spreadsheet, or AI Table write logic here.

The MVP DWS contract baseline is v1.0.57. Treat both compact leaf Schema and leaf Help as authoritative evidence. Record differences between them; do not silently infer missing parameters.

## Source and route models

- `SourceArtifacts` owns the local source, its SHA-256, exact Markdown/JSON/assets, and evidence paths.
- `RouteTarget` owns one of `document`, `sheet`, or `aitable`, a new target title, an optional explicit profile, an optional explicit 1-based SoMark table selector for record routes, and the local evidence directory.
- `RouteResult` owns stage, scoped target identifiers, direct URL, statistics, degradations, warnings, operation ledger, readback evidence, and a structured error.

Accept only explicitly supplied artifact paths. Never inspect adjacent files, conventional result directories, indexes, manifests, or previous output locations. For a local source, require the caller to perform a fresh SoMark parse and pass its Markdown and JSON paths. Allow a source-less document input only when both Markdown and JSON paths are explicitly supplied.

## DWS runner

Build a string argument list and resolve `dws` with the platform executable lookup. Add `--format json` unless an equivalent JSON format argument already exists. Add `--profile` only when the caller provides one. Add `--dry-run` only when requested.

Capture UTF-8 output, enforce a timeout, and parse a final JSON object or array after optional progress text. Return the redacted command, exit code, structured stdout, redacted stderr, duration, and normalized error. Do not turn an exit code of zero into a `verified` business result.

Never add `--yes`, automatically resubmit a confirmation token, guess a profile, or retry. Preserve `retryable`, retry delay, next retry time, hint, and actions in the error result so the caller can make an explicit decision.

## Identifiers and verification

Decode identifiers only from documented response paths for the specific command. Keep `nodeId`, `sheetId`, `baseId`, `tableId`, `fieldId`, and `recordId` distinct. Never use a generic recursive `id` finder.

Route implementations must create first, write second, and read back third. Only route-specific comparison can set `verified`. Transport success leaves the stage at `written` or earlier.

## Redaction and manifests

Recursively redact credentials, authorization headers, cookies, bearer values, and signed temporary URL query strings. Preserve ordinary DingTalk identifiers and unsigned direct links.

Write manifest schema version 1 atomically in the evidence directory. Valid stages are `pending`, `running`, `written`, `verified`, `failed`, and `partial`. Record source and hash, SoMark artifacts, DWS version, scoped target data and direct URL, timings, statistics, degradations, warnings, operation ledger, readback evidence, and structured errors.

## Parallel ownership

- Document work owns only `scripts/somark_dingtalk/document.py`, `test_dingtalk_document*.py`, and its document route report.
- Spreadsheet work owns only `scripts/somark_dingtalk/sheet_*.py`, `test_dingtalk_sheet*.py`, and its spreadsheet route report.
- AI Table work owns only `scripts/somark_dingtalk/aitable_*.py`, `test_dingtalk_aitable*.py`, and its AI Table route report.
- Route work must not change the root or platform `SKILL.md` instructions, common modules, packaging, distribution artifacts, project progress files, or another platform adapter.
