# SoMark Skills

[中文](./README.zh-CN.md)

> Official [SoMark](https://somark.ai) skills collection for document parsing, image OCR, and intelligent extraction — built for AI agent workflows.

## Install

```bash
npx skills add https://github.com/SoMarkAI/skills
```

Works with Claude Code, Cursor, Cline, OpenCode, and [40+ other agents](https://skills.sh).

---

## Skills

| Skill | Description |
|-------|-------------|
| **somark-document-parser** | Parse PDFs, Word, PowerPoint, and images into structured Markdown or JSON |
| **image-parser** | Extract text with bounding-box coordinates from images (OCR + precise location) |
| **document-diff** | Compare two documents and generate a structured diff report showing changes, additions, and deletions |
| **contract-reviewer** | Review contracts for risks, unfair clauses, missing provisions, and key obligations with severity ratings |
| **resume-parser** | Parse resumes and CVs into structured JSON profiles with opinionated candidate assessment |
| **tender-analyzer** | Extract qualification requirements, scoring criteria, deadlines, and submission checklists from procurement documents |
| **paper-digest** | Deeply analyze academic papers into structured research cards covering methods, results, and contributions |
| **financial-report-analyzer** | Extract financial metrics, risk signals, and management commentary from annual reports and earnings releases |
| **pitch-screener** | Screen startup pitch decks from a VC/angel investor perspective — parses the deck, runs background research via web search, and produces a pre-meeting investment memo |

---

## What it does

When you share a document with your AI agent, SoMark parses it into structured Markdown or JSON that the agent can actually reason over — not just OCR'd text, but proper headings, tables, formulas, and layout.

The image-parser skill goes further: it returns every text block with its exact pixel coordinates on the original image, enabling field extraction, region location, and document automation.

**Supported formats:**

| Type | Formats |
|------|---------|
| Documents | PDF, DOC, DOCX, PPT, PPTX |
| Images | PNG, JPG, JPEG, BMP, TIFF, WEBP, HEIC, HEIF, GIF |

**Example triggers:**

- "Parse this PDF for me"
- "Extract the key clauses from this contract"
- "Review this contract for risks"
- "Parse this resume and give me a candidate assessment"
- "Analyze this annual report"
- "Summarize the paper I just uploaded"
- "What changed between these two documents?"
- "Analyze this tender document — what are the qualification requirements?"
- "Convert this document to Markdown"
- "What does this image say?"
- "Extract all text with bounding boxes from this image"
- "Find the invoice amount and its position on the page"

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
