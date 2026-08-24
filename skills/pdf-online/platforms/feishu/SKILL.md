---
name: somark-to-feishu
description: Convert PDF, image, Word, or PPT files through SoMark and create editable Feishu cloud documents, layout-preserving Feishu spreadsheets, or mapped records in Feishu Bitable. Use when a user asks to parse an unstructured or scanned document with SoMark and import the result into Feishu, or when existing SoMark Markdown and JSON need a Feishu-compatible document, spreadsheet, or Bitable output.
---

# SoMark To Feishu

Convert SoMark Markdown and JSON into a Feishu cloud document, electronic
spreadsheet, or Bitable record payload. Preserve SoMark's original Markdown
and JSON. Use the route that matches the user's target rather than forcing all
content through a cloud document.

## Workflow

1. Resolve the source:
   - For a raw PDF, image, Word, or PPT file, the Agent must invoke the
     separately installed official `somark-document-parser` Skill exactly once
     in the task and request both Markdown and JSON. Feishu adapter scripts do
     not include or discover the parser.
   - Bypass parsing only when the user explicitly supplies the exact matching
     Markdown and JSON paths. Never discover or reuse adjacent, historical,
     same-name, hashed, indexed, or recently modified results.
   - When a source path and an explicit matching pair are both supplied, treat
     the pair as authoritative formatting/structure artifacts and use the
     source path only as provenance; state this choice instead of silently
     switching inputs.
2. Select the target route before writing:
   - Default to a Feishu cloud document when the user asks to read, edit, or
     share a document.
   - Use a Feishu electronic spreadsheet for layout-preserving tables.
   - Use Bitable only when the user asks for structured records, views, or
     workflow-ready data. Default to creating a new Base. Use an existing Base
     only when the user's prompt explicitly asks to write into one.
3. Use the official general SoMark Skill defaults. Do not modify or replace
   `somark-document-parser`, and do not change parser settings based on the
   destination platform unless the user explicitly asks for a different
   profile. The parser returns Markdown + JSON with image URLs; platform-only
   downloading and upload belong to this adapter:

```json
{
  "element_formats": {
    "image": "url",
    "formula": "latex",
    "table": "html",
    "cs": "image"
  },
  "feature_config": {
    "enable_title_level_recognition": false,
    "enable_inline_image": true,
    "enable_table_image": true,
    "enable_image_understanding": true,
    "keep_header_footer": false
  }
}
```

4. For the cloud-document route, use the normal Markdown + JSON parser output.
   Do not request ZIP; cloud documents import URL-based images and do not need
   local table-image assets. Generate the compatibility draft:

```powershell
python <skill-dir>\scripts\convert_to_feishu.py `
  --markdown <somark.md> `
  --json <somark.json> `
  --title "<Feishu document title>"
```

5. When the converter exits successfully and the generated `_feishu.md` exists,
   start the Drive import immediately. The converter writes the compatibility
   draft and manifest in the same invocation. Do not pause to inspect the
   manifest, summarize transformation statistics, or ask the user to confirm
   before starting the import. A blank Markdown/JSON payload with a successful
   SoMark response is still a successful parse; stop only for an actual
   conversion or import error.
6. Import only the generated `_feishu.md` through the Drive import route:

```powershell
lark-cli.cmd drive +import --file <relative-path-to-feishu.md> `
  --type docx --name "<title>" --as user
```

7. When import returns `ready=false`, run the returned `next_command`. Wait
   until a final document URL and token are available.
8. Immediately send the document URL to the user so they can start browsing
   the imported content. Clearly state that the base document is available and
   that background enhancement is still applying image captions, native table
   images/equations, formula headings, post-import formula validation and
   repair, and other planned Block API updates. Do not describe the document as fully
   processed yet. Formula validation must not delay this preview boundary.
9. After sending the URL, read the generated manifest and continue applying
   the native-block plan. If the manifest is not ready or its targets do not
   match the imported document safely, keep the base document available and
   report that enhancement was skipped or stopped. Use `--dry-run` first when
   testing a new document shape, then run the command without it:

```powershell
python <skill-dir>\scripts\apply_feishu_blocks.py `
  --document <Feishu-document-URL-or-token> `
  --manifest <source_feishu.manifest.json> `
  --dry-run
```

   The updater treats each enhancement type independently. Body-image matching
   excludes images already nested in table cells. It uses surrounding-text
   anchors for body images, writes captions only to uniquely matched Image
   Blocks, and skips ambiguous body images. Table-cell image locations come
   from JSON row/column coordinates: first preserve the historically verified
   isolated image paragraph so Drive can import it natively; only when the
   target cell contains no native image, download and insert the image into
   that exact TableCell. Never insert a duplicate table image.
   A table-count mismatch skips table formulas, while uniquely located image
   captions and formula headings may still continue. Only a request-integrity
   conflict stops all writes. It also audits native equation elements against
   the manifest, repairs uniquely located formula paragraphs whose Markdown
   boundaries were damaged by Drive import, rebuilds uniquely located display
   formulas that retained raw `$$` delimiters, and fetches the blocks again
   after writing to verify that no planned formula is still missing.
10. After the Block API stage finishes, tell the user whether enhancement was
   applied, partially applied, skipped, or stopped by a safety mismatch. Mention
   only actual conversion/upload failures. An image whose source JSON has no
   description is intentional: do not call it missing, skipped, ambiguous, or
   degraded in the final report. A successful import with degraded elements is
   "created with degradation", not a fully faithful success.

## Conversion Rules

The bundled scripts perform deterministic V0.2.2 compatibility processing:

- Preserve an empty successful SoMark result without adding a second content
  gate in the parser layer.
- Convert `footnote` blocks in source order to numbered editable endnotes such
  as `> [尾注 1] ...`, matching the DingTalk rule. When the JSON
  footnote marker is empty and has a `bbox`, merge consecutive following text
  blocks contained by that region into one footnote. Stop at the first text
  block outside the region or the first non-text block. Without geometry,
  conservatively use only the immediately following text block.
- Convert multi-line `choice` blocks to Markdown unordered lists (`- ...`),
  preserving the original option labels.
- Keep image understanding enabled, import images with an empty Markdown
  `alt`, and store each description plus its nearest preceding/following text
  anchors in the manifest. After import, write the description to the
  corresponding native Image Block `caption.content`. If Drive import drops an
  body image, apply only uniquely anchored captions. Use a source description
  exactly when JSON supplies one; never synthesize a caption when it does not.
- In HTML table cells, isolate every Markdown image with blank lines before and
  after it to preserve the live-tested native Drive import behavior. Also store
  its exact JSON-derived row, column, URL, and description for a TableCell API
  fallback. Empty descriptions are valid and never trigger a warning.
- Preserve valid code fences, close an unmatched fence, and recover an
  unfenced JSON `code` block only when its exact text is present.
- Convert numbered headings wrapped entirely in formula syntax, such as
  `### $1.1\quad Title$`, into ordinary Markdown headings so Feishu retains
  them in the document outline.
- Record any remaining Markdown heading that contains an inline formula. If
  Drive import turns it into a Text Block, recreate it at the same position as
  the original Heading Block level while reusing its native equation and text
  elements, then delete the degraded paragraph.
- Preserve HTML tables and their merged-cell structure. Only remove a
  conflicting header `rowspan` when it would cause Feishu to drop the next
  complete data row.
- Wrap safe bare superscript fragments such as `^{[42]}` and `^{a,b,c,*}` as
  inline formulas outside code and existing formulas; apply the same rule in
  HTML cells. Record these repaired superscripts plus already balanced,
  explicit `$...$` and `$$...$$` formulas. After import, locate each target
  cell uniquely and replace the marker with a native `equation` text element
  through the Block API.
- Do not normalize Unicode superscripts, rewrite ordinary table text, or
  attempt to unify table fonts. Leave other malformed or unbalanced formulas as raw text rather than
  guessing a repair.
- Convert supported `\ce{...}` reactions to standard Feishu-compatible KaTeX
  with upright chemical symbols, subscripts, arrows, and reaction conditions.
  Convert a simple bracketed polymer repeat such as `[CH2CH2]n` to a native
  repeat-unit formula with visible terminal chain bonds, for example
  `\left[\mathrm{-CH_{2}-CH_{2}-}\right]_{n}`.
  Fall back to readable text only when the syntax cannot be converted safely.
- Inside balanced Markdown formulas only, convert raw `<` and `>` comparisons
  to `\lt` and `\gt` so Feishu's Markdown/HTML import layer cannot consume the
  formula boundary. Record the expected equations and comparison-formula
  paragraphs in the manifest for post-preview validation and targeted repair.
- Remove the first H1 only when it exactly equals the requested Feishu title.

Do not rewrite OCR prose, infer missing model content, or change heading
levels. Treat vertical-text and alignment recognition failures as model issues.

## Output Contract

For `source.md`, the script creates:

- `source_feishu.md`: Feishu import draft.
- `source_feishu.manifest.json`: source hashes, transformation counts,
  warnings, degradation records, validation status, image-caption plans,
  table-formula and formula-heading plans, and structural-element mappings for
  footnotes and choices.
- `source_feishu.manifest.block-report.json`: Block API validation or write
  report.

Keep these files with the original Markdown and JSON for auditability.

## Feishu Sheets Workflow

Use this route when the user asks for a Feishu electronic spreadsheet rather
than a Feishu cloud document. It consumes SoMark JSON `table` blocks directly;
do not round-trip them through Markdown.

1. Before spending a SoMark parse call, run `lark-cli auth status --json
   --verify` once. If Sheets authorization is missing, complete only the
   required user authorization first. Do not repeat the same auth check after
   parsing unless a Feishu command returns an authentication error.
2. Parse once with the official normal Markdown + JSON output and
   `element_formats.image=url`. Do not request ZIP or require changes to the
   generic parser. The table JSON is the source of truth for image location;
   Markdown image URLs are only a fallback when they occur inside the same
   table cell.
3. Generate the layout payload exactly once. An explicitly supplied local
   asset directory remains an optional cache/compatibility input, but is not
   required. Never infer an asset directory from the JSON filename or inspect
   neighboring folders. Do not download table images or inspect the manifest before
   creating the workbook:

```powershell
python <skill-dir>\scripts\prepare_sheets_payload.py `
  <somark.json> <output-dir>
```

4. As soon as `sheets_payload.json` exists, create the workbook with the
   preview-only helper. It writes content only, saves the exact workbook token
   in `preview_checkpoint.json`, and exits before styles, merges, sizes, or
   images can run. Do not pause to read `manifest.json`, summarize statistics,
   run a dry-run, or ask the user to confirm:

```powershell
python <skill-dir>\scripts\create_sheets_preview.py `
  <output-dir> `
  --title "<title>"
```

   The helper owns the required payload-directory working directory and the
   quoted `@sheets_payload.json` argument. Do not replace it with a combined
   create-and-enhance command.

5. Once Feishu returns a workbook URL, send it to the user immediately in an
   intermediate update. Say that the content-only workbook is available for
   preview and that styles, merged cells, and table images are still being
   applied. Continue in the same task without waiting for a reply. This
   is a hard response boundary: do not run another tool before emitting the
   URL. If the host buffers intermediate assistant messages, end the response
   with the URL and resume enhancement on the next turn instead of withholding
   the preview until all work is complete.
6. After sending the URL, apply the prepared styles, row and column sizes,
   merged cells, and table images. The enhancer first reuses a matched local
   file when one is explicitly available; otherwise it downloads the HTTP(S)
   URL recorded for that exact JSON table cell, caches it, and calls the native
   cell-image API. This mode never rewrites worksheet values:

```powershell
python <skill-dir>\scripts\apply_sheets_payload.py `
  <output-dir> `
  --preview-checkpoint <output-dir>\preview_checkpoint.json `
  --enhance-only
```

7. The enhancer batches style and merge operations, retries Feishu rate limits,
   and uploads images separately with `+cells-set-image`. One image download or
   upload failure must not stop the other images or invalidate the usable
   content-only workbook; preserve the URL text and report a local degradation.
   If it is interrupted,
   rerun the same `--enhance-only` command. Add `--reset-merges` only when a
   previous enhancement partially applied merge ranges:

```powershell
python <skill-dir>\scripts\apply_sheets_payload.py `
  <output-dir> `
  --preview-checkpoint <output-dir>\preview_checkpoint.json `
  --enhance-only `
  --reset-merges
```

8. Keep post-preview checks small. A successful enhancer result plus one
   `+workbook-info` call is sufficient for the normal path. Use targeted
   `+cells-get`, image readback, manifest inspection, or a dry-run only when the
   enhancer reports a mismatch, the document shape is new and risky, or the
   user explicitly asks for verification. Never delay the preview link for
   these checks. Report enhancement failures without withdrawing the usable
   content-only workbook.

### Spreadsheet Conversion Rules

- Create exactly one worksheet per SoMark `table` element. Do not generate
  separate layout, data, hybrid, or analysis copies.
- Preserve expanded cells, merge ranges, borders, wrapping, and estimated column
  widths for visual comparison with the source document.
- Before applying merges, keep cell values and skip only a merge range that is
  invalid, overlaps an already accepted range, or would discard a non-anchor
  value. Continue all other enhancements and report every skipped merge as a
  local degradation.
- Write every non-empty cell with the `object` dtype. Do not infer or write native
  number, date, time, percentage, or formula values.
- Convert only supported simple LaTeX fragments to readable Unicode text, such as
  `m³`, `4°`, `φ`, `≥`, and `±`. Preserve unsupported complex LaTeX verbatim.
- Do not guess-correct SoMark recognition output such as `_ 130`.
- Treat the result as a layout-restoration workbook for viewing, checking,
  filling, and archiving. Do not claim that it is ready for calculation,
  numeric filtering, sorting, charts, or pivots.

## Feishu Bitable Workflow

Use this route when the user wants records for a Base view, calendar, Gantt
chart, form, dashboard, or workflow. Default to a new Base for every request.
Use an existing Base only when the user explicitly says to write into an
existing Base or supplies a Base URL/token as the destination. Do not reuse a
recent Base merely because it is available.

Do not ask the user to confirm headers, field mapping, or a destination Base
before SoMark parsing. After a successful SoMark JSON result exists, treat its
HTML `table.content` as the sole source of truth for Bitable headers. Never
open, render, screenshot, visually inspect, or run another OCR pass on the
original PDF/image to check headers. Do not use the Markdown output as a second
header source. If the SoMark header is incomplete or duplicated, report that
result after parsing; do not fall back to OCR unless the user explicitly asks
for a separate OCR comparison.

Briefly remind the user, without waiting for a reply, that clear headers improve
Bitable field mapping. Missing data cells and blank rows are supported. Empty
headers become `文本`, `文本2`, and so on; duplicate headers retain the first name
and receive numeric suffixes. Preserve the original header and column index in
the mapping audit. These repairs are warnings, not prerequisites.

Use the normal JSON parser output. The implemented mapping supports `text`,
`number`, and `date` fields and does not write attachment/image fields. If
Bitable attachment mapping is implemented later, acquire its files inside this
adapter from the source URLs rather than changing the generic parser.

This route is create-only. It deliberately does not generate `Source Key`,
hash the source file, read existing records, update matching records, or keep a
local record-ID map. Repeating the same task creates another set of records.
Duplicate-upload management belongs to the calling product or user workflow,
not to this one-time document conversion path.

### Default new-Base path

1. Start SoMark parsing immediately. Do not inspect the original file before or
   after this parse for header discovery.
2. Run `auto-prepare` directly on the SoMark JSON. It derives fields from the
   selected SoMark header, records any safe-name repairs, conservatively infers
   `text`, `number`, or `date` from the parsed cells, writes the mapping, and prepares the records
   in one command. It does not perform OCR or open the source file:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py auto-prepare `
  --json <somark.json> `
  --source-file <original-file.pdf> `
  --mapping-output <field-mapping.json> `
  --output <records.json>
```

   Use the only table and row 1 as its header. With multiple tables, stop after
   listing the reported table candidates and ask the user to choose; rerun with
   `--table-index`. Use `--header-row` when the SoMark JSON clearly identifies a
   different header row. Determine these only from JSON. Record fields remain
   optional, completely empty rows are preserved, and no `required` field may
   stop creation. If `requires_confirmation=true`, show the mapping warnings;
   when the user confirms or already insists on Bitable, reuse the prepared
   mapping and continue. Use manual `prepare` only when the user explicitly provides
   a custom mapping or an existing Base requires different target field names.

3. Create a new Base with an empty first table. The script derives Feishu
   `text`, `number`, and `datetime` fields from the mapping and saves the
   returned URL, Base token, and table ID in `<base-target.json>`. Do not add an
   internal `Source Key` field. Do not send or open the Base URL at this stage:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py create-base `
  --mapping <field-mapping.json> `
  --name "<source name> - SoMark" `
  --table-name "数据" `
  --output <base-target.json>
```

   `create-base` saves a sanitized successful CLI response beside the target
   before decoding resource IDs. It reads the new table ID from
   `data.table.table_id` or `data.table.id`, never from a recursive generic
   `id` search. If table ID decoding or recovery fails after the remote Base
   was created, rerun this exact command with the same `--output` path. It will
   reuse the saved response, query `+table-list`, match `--table-name` exactly,
   and write the target without calling `+base-create` again. Do not change the
   output path, delete the response snapshot, or retry with a fresh Base.

4. Start record creation with `start-create`. This command writes the first
   batch of up to 200 records before returning `preview_url`, and writes any
   unsubmitted records to `<remaining-records.json>`. Do not run a dry-run or
   a remote pre-read because either delays first usable content:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py start-create `
  --target <base-target.json> `
  --payload <records.json> `
  --remaining-output <remaining-records.json>
```

5. Only after `start-create` returns `phase=first_batch_written` and a positive
   `created_record_count`, send its `preview_url` to the user in a progress
   message. Say that the first records are available and that remaining
   records, views, or other finishing work may still be in progress. This is a
   hard response boundary: emit the URL before another tool call. If the first
   batch fails, report the failure and do not expose the still-empty Base.
6. If `remaining_record_count` is greater than zero, continue with the saved
   continuation payload. Skip this command when the count is zero:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py create `
  --target <base-target.json> `
  --payload <remaining-records.json>
```

7. The writer performs no remote pre-read and writes at most 200 records per
   batch. When writing completes, report the total created count and any schema
   assumptions. If continuation fails, keep the already shared partially
   filled Base available and report the failed stage. Configure calendar,
   Gantt, or other views only after the required typed fields exist.

Do not use the combined `run` command for the default new-Base path because a
single long-running command prevents the Agent from sharing the preview link
after the first successful record batch and before continuation writing.

### Explicit existing-Base path

Use this path only when the user's prompt explicitly names an existing Base as
the destination. Parse the source first, then resolve the Base URL/token, read
its real fields, and build a compatible mapping. Generate `<records.json>` with
`prepare`, then write it directly with the existing Base token and table ID:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py create `
  --base-token <base-token> `
  --table-id <table-id> `
  --payload <records.json>
```

For an explicitly selected existing Base, `run` remains available after the
mapping has been built from the parsed result:

```powershell
python <skill-dir>\scripts\somark_to_bitable.py run `
  --json <somark.json> `
  --mapping <field-mapping.json> `
  --source-file <original-file.pdf> `
  --payload-output <records.json> `
  --base-token <base-token> `
  --table-id <table-id>
```
