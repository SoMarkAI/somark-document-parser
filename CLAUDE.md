# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SoMark Skills — a collection of AI agent skills for document parsing and image OCR, powered by the [SoMark](https://somark.ai) API. Skills are installable via `npx skills add https://github.com/SoMarkAI/skills` and work with Claude Code, Cursor, Cline, and other agents.

## Repository Structure

```
skills/
  image-parser/                    # OCR skill: extracts text + bounding-box coordinates from images
  somark-document-parser/          # Document parsing skill: PDF/Word/PPT/images → Markdown/JSON
  document-diff/                   # Compare two documents, generate structured diff report
  contract-reviewer/               # Contract risk review with severity ratings
  resume-parser/                   # Resume → structured JSON + candidate assessment
  tender-analyzer/                 # Extract requirements, scoring criteria, checklists from RFPs
  paper-digest/                    # Academic paper → structured research card
  financial-report-analyzer/       # Annual report → financial metrics, risk signals, management commentary
  pitch-screener/                  # VC/angel pre-meeting investment memo (deck parse + web background research)
```

Each skill has three files: `SKILL.md` (frontmatter with name/description/metadata + usage instructions), `_meta.json` (slug + version), and a Python script.

## Key Architecture Differences Between Skills

- **image-parser** uses the **sync** SoMark endpoint (`/extract/acc_sync`) and stdlib-only (`urllib`). No external dependencies.
- All other skills use the **async** SoMark endpoint (`/extract/async` + `/extract/async_check` polling) and require `aiohttp`.
- **pitch-screener** additionally uses web search (via available MCP tools) for background research after parsing.

## Running the Scripts

```bash
# Image parser (single file)
python skills/image-parser/image_parser.py -f <image_path> -o <output_dir>

# Image parser (directory)
python skills/image-parser/image_parser.py -d <image_dir> -o <output_dir>

# Document parser (single file)
python skills/somark-document-parser/somark_parser.py -f <file_path> -o <output_dir>

# Document parser (directory)
python skills/somark-document-parser/somark_parser.py -d <dir_path> -o <output_dir>
```

Both scripts require the `SOMARK_API_KEY` environment variable.

## Conventions

- SKILL.md frontmatter uses the `metadata.openclaw` schema: emoji, `requires.env`, and `primaryEnv`.
- Error messages and CLI output in the Python scripts are in Chinese.
- The skill name in SKILL.md frontmatter must match the directory name (e.g., `name: image-parser` for `skills/image-parser/`).
