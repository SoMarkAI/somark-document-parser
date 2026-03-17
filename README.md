# somark-document-parser

> Parse PDFs, images, Word, and PowerPoint files into clean Markdown or JSON using [SoMark](https://somark.ai) — the document intelligence API built for AI workflows.

## Install

```bash
npx skills add https://github.com/SoMarkAI/somark-document-parser
```

Works with Claude Code, Cursor, Cline, OpenCode, and [40+ other agents](https://skills.sh).

---

## What it does

When you share a document with your AI agent, SoMark parses it into structured Markdown or JSON that the agent can actually reason over — not just OCR'd text, but proper headings, tables, formulas, and layout.

**Supported formats:**

| Type | Formats |
|------|---------|
| Documents | PDF, DOC, DOCX, PPT, PPTX |
| Images | PNG, JPG, JPEG, BMP, TIFF, WEBP, HEIC, HEIF, GIF |

**Example triggers:**

- "Parse this PDF for me"
- "Extract the key clauses from this contract"
- "Summarize the paper I just uploaded"
- "Convert this document to Markdown"
- "What does this image say?"

---

## Setup

Get an API key at [somark.tech](https://somark.tech), then set it as an environment variable:

```bash
export SOMARK_API_KEY=sk-your-api-key
```

Or add it to your agent's settings. The skill will guide you through setup on first use.

**Free quota:** SoMark offers a free tier. Visit the [purchase page](https://somark.tech/workbench/purchase) and follow the instructions there to claim it.

---

## Why SoMark

Most agents struggle with documents because raw PDF/image data loses structure. SoMark preserves:

- **Heading hierarchy** — agents can understand document sections correctly
- **Tables** — fully reconstructed instead of flattened into plain text
- **Formulas and diagrams** — converted to LaTeX or described accurately
- **Multi-column layouts** — reading order is preserved

The result: your agent gives accurate, context-aware answers instead of hallucinating from garbled text.

---

## Limits

| Constraint | Limit |
|------------|-------|
| Max file size | 200 MB |
| Max pages | 300 pages |
| QPS per account | 1 |

---

## License

MIT
