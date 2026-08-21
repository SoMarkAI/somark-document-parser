---
name: somark-to-dingtalk
description: Use SoMark to parse local PDF, image, Word, or PPT files once and create editable DingTalk documents, online spreadsheets, or AI Table records. Also use when a user explicitly supplies matching SoMark Markdown and JSON artifacts.
---

# SoMark to DingTalk

For a raw local source, the Agent must first call the separately installed
official `somark-document-parser` Skill exactly once, then give its exact
Markdown-and-JSON pair to this adapter. If the user explicitly supplies that
pair, skip parsing. DingTalk adapter scripts do not include or discover the
parser. Keep destination-specific image acquisition inside this adapter.

## Public entry point

After the parser Skill returns, use the unified CLI with the exact artifacts:

```text
python scripts/convert.py publish [--source <path>] --markdown <somark.md> --json <somark.json> --route document|sheet|aitable --title <title> --profile <profile> --evidence-dir <dir> --mode fast|strict [--table-index <n>] [--preview-first]
python scripts/convert.py resume --manifest <evidence-dir>/publish_manifest.json --profile <same-explicit-profile>
```

- Always supply `--markdown` and `--json`. The adapter rejects `--source`-only
  publishing and never starts, discovers, or bundles a SoMark parser. `--source`
  is optional provenance only when the exact artifact pair is also supplied.
- `publish` executes by default. Use `--plan-only` only for an explicitly requested local plan or test.
- `--profile`, `--evidence-dir`, and `--mode` are explicit orchestration inputs. `fast` is the ordinary user-visible path; `strict` retains full route verification.
- Standard output is newline-delimited JSON lifecycle events only. Keep diagnostics on standard error and never mix parser or DWS prose into the event stream.
- Flat route modules are lazy-loaded only after parsing completes and planning begins.
- `resume` replays persisted, undelivered events. For a preview-first spreadsheet it applies deferred layout work to the saved workbook; for a partial document it re-enters the persisted node and rebuilds the remaining table-repair plan. Both require the same explicit profile and must not reparse the source or recreate a destination.

## Route

1. Inspect the source and the user's requested destination.
2. Choose exactly one route: `document`, `sheet`, or `aitable`.
3. Prefer `document` for narrative and layout-oriented content, `sheet` for grid-oriented tables and formulas, and `aitable` for typed records and attachments.
4. Ask before proceeding when the requested destination is ambiguous or when the route would materially change fidelity.

The document, spreadsheet, and AI Table implementations are flat modules under
`scripts/somark_dingtalk/`: `document.py`, `sheet_*.py`, and `aitable_*.py`.

## Decide SoMark outputs before parsing

- Every route requests the official Markdown + JSON output once with `element_formats.image=url`. Do not modify `somark-document-parser`, request ZIP, or make a second parse for image assets.
- `document`: preserve the public HTTP(S) image URLs because DingTalk documents accept them directly.
- `sheet`: build the workbook from JSON first. After the preview event, download only image URLs found in exact JSON table cells and upload them through the native cell-image API. Reuse an explicitly supplied local asset directory when available, but never require one.
- `aitable`: the current mapping has no attachment field consumer, so keep URL/text data only. Add adapter-local downloading later if attachment mapping is implemented.
- Keep `keep_header_footer=false` for every parse. The document adapter also omits header and footer blocks from explicitly supplied JSON.

## Source policy

- For every local PDF, image, Word, or PPT source, call the official
  `somark-document-parser` Skill exactly once in that publish task, then pass
  its exact Markdown and JSON outputs here. Never search for, inspect, or
  prefer earlier SoMark outputs, result directories, indexes, manifests, or
  adjacent Markdown/JSON files.
- Bypass parsing only when the user explicitly supplies the exact paths of both a SoMark Markdown file and its matching JSON file. Require both files, use exactly those paths, and do not discover substitutes. A sheet route may additionally reuse an explicit assets directory as a cache, but it is optional.
- When a raw source path and an explicit matching pair are both supplied, use the pair as the authoritative content artifacts and the source only for provenance and hashing. State this precedence; never silently switch to another result.
- Do not treat files found beside a local source as user-specified inputs.
- Never call the SoMark API from the shared foundation.
- Preserve the source hash and artifact paths in the manifest.

## Fast-start execution

1. Treat an explicit request to use or rerun SoMark as authorization for one parse call; state that the call consumes quota, but do not ask for duplicate confirmation. If the user did not explicitly authorize SoMark, follow the parser skill's quota-confirmation rule.
2. After validating the source path and authorization, emit `parse_started` and start exactly one route-appropriate SoMark request immediately.
3. Do not block parsing on historical-result inspection, a separate `dws --version` call, or repeated dependency checks. Check Python dependencies once per runtime. Let the route perform its own DWS version validation.
4. Prepare the DingTalk profile and authentication concurrently with parsing when possible, or immediately after parsing starts. Never delay the parse solely for DingTalk preparation.
5. In a host known to restrict network access, request the required network permission before the first SoMark call. Do not make an expected-to-fail request merely to discover the restriction, and never bypass platform approval.
6. Persist each event before delivery. For successful documents, emit `parse_started`, `parse_completed`, `plan_completed`, `postprocess_started`, `postprocess_completed`, `preview_ready`, and exactly one terminal event. If refinement stops, emit `postprocess_interrupted` with resumability evidence instead of claiming completion. Do not expose the document link before post-processing finishes. Keep the existing preview timing for spreadsheets and AI Tables. During a long phase, send a concise progress update at least every 30 seconds.
7. For an ordinary conversion, read only this route guidance. Read `references/common-contract.md` only when implementing or changing route code; do not load unrelated route references.

## Execute safely

- Invoke `dws` with an argument list and JSON output. Never use a shell command string.
- Persist redacted argv, business-call sequence, raw stdout/stderr, every decoded JSON value, and the selected JSON stream for each DWS call. Accept prefixed progress, NDJSON, multiple JSON values, and stderr JSON errors.
- Require `dws` v1.0.57 for the frozen MVP contracts.
- Use an explicitly selected profile, or the one uniquely current profile. Never guess or select the first profile.
- Create a new target by default. Overwrite an existing DingTalk destination only when the user explicitly asks for repair and after the DWS overwrite dry-run and confirmation flow.
- Keep DingTalk identifiers scoped to the response field that owns them. Never recursively search for a generic `id`.
- Surface confirmation requirements as structured results. Never add confirmation flags or retry automatically.
- Treat command completion as transport evidence, not business verification.

## Document conversion behavior

- Preserve public HTTP(S) image URLs from the SoMark Markdown; do not download and re-upload those images.
- Keep operational Markdown and record payloads unchanged, including signed image URLs needed by DingTalk. Apply redaction only to manifests, events, commands, errors, summaries, and other diagnostic evidence; never publish a redacted payload.
- Preserve source table markup except for one proven compatibility normalization: make each Markdown table-cell image an isolated paragraph with blank lines so the native importer can recognize it. After creation, use source table order and cell text as anchors, read only table JSONML, and compile supported table rich text into native DingTalk nodes. Handle combined bold, italic, underline, strike, color, highlight, font, size, character spacing, superscript, subscript, links, inline code, line breaks, formulas, HTTP(S) cell images, paragraph alignment, cell fill, and vertical alignment. Preserve existing native rich text when it already satisfies the source.
- For every table-cell image whose source JSON Markdown `alt` is nonempty, render that exact description as editable text directly below the native image in the same cell. When the source `alt` is empty, render no description. Never synthesize or borrow a description.
- DingTalk block-update JSONML accepts table-cell image `src` but rejects `img.alt`; keep the description only in the following editable text node. Emit `br` without a text child.
- Treat citation fragments such as `^{[48]}` and `$^{[48]}$` as native superscript leaves displaying `[48]`. Treat safe ordinary fragments such as `^{a,b,c,*}` as inline formulas outside code/existing formulas, and as native superscript leaves inside table cells. Treat other balanced `$...$` table fragments as native formula tags. Leave malformed formulas and Unicode superscript characters unchanged instead of guessing.
- Convert SoMark `footnote` elements in source order to numbered editable endnotes rendered as Markdown blockquotes, for example `> [尾注 1] ...`. When a footnote marker is empty, use adjacent text contained by its JSON region; without geometry, use at most the immediately following text block. Record the label and source adjacency in the manifest, and treat this as an editable degradation rather than a native DingTalk footnote.
- Update only matched table cells or paragraphs with stable UUIDs. Refuse ordinary cell updates when source/target table counts, cell counts, or normalized cell text do not match. For an image source cell only, when source and target logical cell counts are equal, use its exact JSON row-major position even if DingTalk dropped the literal Markdown marker; never use this exception when counts differ.
- For `figure`, `stamp`, `qrcode`, and chemical images, add an adjacent description only when the original JSON content is nonempty. Never invent a generic description or visual-review warning for an image whose source description is empty. Do not mention empty source descriptions in the final report; report only actual image loss or upload/write failures.
- Omit headers and footers by default.
- Keep the route's internal preview callback for target persistence, but buffer its `direct_url` in the public publisher.
- Emit and send `preview_ready` only after table refinement and targeted readback finish successfully. Do not expose a document link for a `partial` or `failed` conversion.
- Checkpoint table refinement after every block response. If a write may have succeeded but its response cannot be decoded, read the block back before retrying; a confirmed write must not be duplicated.
- Treat the local character-based chunk count as an estimate only. Persist DWS `chunksWritten` as the reported write count; the two splitters are not an equality invariant. A successful create plus remote late-content/readback evidence determines body completeness.
- Full Markdown + JSONML + Block business readback is opt-in through `verify=True`; do not enable it for ordinary conversion.
- Local conversion summaries and manifests are diagnostic artifacts only and must not trigger extra remote calls or be surfaced as a long degradation report by default.
- Never append integrity markers, audit hashes, adapter signatures, or other diagnostic text to the user-visible document. Keep verification evidence only in local manifests and platform readback.

## AI Table conversion behavior

- Accept the normal SoMark JSON `pages[].blocks[].type=table` structure directly and derive records from its HTML table. Preserve the prepared `create_records` + explicitly referenced `field_mapping.json` path for backward compatibility, but never discover a neighboring mapping file.
- Use the only SoMark table. With multiple tables, return the table candidates and ask the user to choose; rerun with `--table-index`. Never guess.
- Preserve every SoMark field name exactly. Never repair, normalize, translate, or replace a parsed header or cell value for content correctness.
- Choose DingTalk field types for display and view behavior. Convert recognizable dates to `date`, progress values such as `75`, `75%`, or `0.75` to native `progress` values in the `0..1` range, finite categories to `singleSelect`, and genuine metrics to numeric types. Keep identifiers and unresolved people or relations as text.
- Do not create raw shadow fields or duplicate source fields. Do not add required flags, validators, validation rules, date ranges, or other input constraints.
- Treat mapping `required` annotations as non-binding hints. Allow fields and cells to be absent, allow blank records, and allow a start-date field without an end-date field. Because DWS v1.0.57 rejects an empty `cells` object, transport a completely blank record with an empty string in the writable primary field; keep it visually blank and treat missing, null, and empty-string primary values as equivalent during readback.
- Treat layout signals such as merged cells, repeated headers, strong visual styling, or coordinate formulas as advisory route risks, not AI Table write blockers. `publish --route aitable` is already an explicit destination choice: keep recommending `sheet` in the plan when it would preserve layout better, but continue the AI Table write using the physical grid and text degradation without requiring a separate force flag.
- Convert cells independently. When a value cannot be represented by the selected DingTalk field type, omit only that cell, record a local degradation, and continue creating the remaining fields and records. Never downgrade an otherwise useful date or progress column because one cell is malformed.
- Fail only for structural problems that make conversion impossible, such as unreadable artifacts, invalid JSON structure, no fields anywhere, a source-hash mismatch, or an unrecoverable DingTalk write error.
- In ordinary `fast` mode, keep the business path to at most five DWS calls: create base, create table, read fields, create records in serial batches, and aggregate record readback. Skip base readback and schema/payload dry-runs. If table creation fails, allow one controlled dry-run diagnostic before returning failure.
- Emit the preview event after record identifiers are persisted and before aggregate record readback. Preserve the slower preflight and verification flow in `strict` mode.

## Spreadsheet conversion behavior

- Invoke `publish --route sheet --preview-first` for the normal user-visible
  path. The command exits after verified base-cell writes and emits
  `preview_ready` followed by `preview_paused`. Send `direct_url` to the user
  before another tool call. Then run `resume` with the saved
  `publish_manifest.json` and the same profile to apply the deferred layout
  work on that exact workbook. Do not combine the two commands into one shell
  invocation.
- Create exactly one worksheet for every SoMark `table` block. Do not join table blocks merely because they appear on consecutive pages with the same column count; cross-page reconstruction belongs to SoMark parsing.
- Preserve worksheet boundaries and create every planned worksheet before writing data. Keep DingTalk worksheet names below 31 characters, add collision-safe numeric suffixes, and retain the full source title in the local plan/manifest.
- Write strings as strings. Persist worksheet identifiers and emit the preview event after base cell writes are verified, before downloading any remote image.
- In post-processing, reuse a matched local image when available; otherwise download the HTTP(S) source recorded for the exact JSON table cell and upload it with the native cell-image API. Upload images before applying merges so merge operations cannot invalidate image anchors.
- A single download or upload failure is a local degradation: preserve the URL text, continue with other images, and keep the already usable workbook link.
- Treat merges, styles, widths, image downloads, and image uploads as post-processing.
- Before applying merges, preserve cell values and skip only an invalid range, a range overlapping an already accepted merge, or a range that would discard a non-anchor value. Continue the remaining writes and record each skipped merge as a local degradation.

## Verify

- For ordinary document conversion, perform no broad automatic business readback. When supported table rich text is detected, perform one targeted table read, update only affected blocks, and perform one targeted table readback. When no table rich-text candidates exist, make no table read or block-update calls.
- When explicit full verification is requested, read back the created destination with the route-specific read commands.
- Record readback evidence, statistics, warnings, degradations, and operation ledger entries.
- Mark the manifest `verified` only after route-specific validation succeeds.
- Use `partial` or `failed` when write or readback evidence is incomplete.

Read [references/common-contract.md](references/common-contract.md) before implementing or changing a route.
