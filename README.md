# somark-document-parser

> Parse PDFs, images, Word, and PowerPoint files into clean Markdown or JSON using [SoMark](https://somark.ai) — the document intelligence API built for AI workflows.
>
> 使用 [SoMark](https://somark.ai) 将 PDF、图片、Word、PPT 等文档解析为干净的 Markdown 或 JSON —— 专为 AI 工作流打造的文档智能 API。

## Install / 安装

```bash
npx skills add https://github.com/SoMarkAI/somark-document-parser
```

Works with Claude Code, Cursor, Cline, OpenCode, and [40+ other agents](https://skills.sh).

兼容 Claude Code、Cursor、Cline、OpenCode 及 [40+ 其他 AI 编程助手](https://skills.sh)。

---

## What it does / 功能介绍

When you share a document with your AI agent, SoMark parses it into structured Markdown or JSON that the agent can actually reason over — not just OCR'd text, but proper headings, tables, formulas, and layout.

当你向 AI 助手发送文档时，SoMark 会将其解析为结构化的 Markdown 或 JSON，让 AI 真正"读懂"文档 —— 不只是识别文字，而是完整还原标题层级、表格、公式和排版结构。

**Supported formats / 支持格式：**

| Type / 类型 | Formats / 格式 |
|------------|---------------|
| Documents / 文档 | PDF, DOC, DOCX, PPT, PPTX |
| Images / 图片 | PNG, JPG, JPEG, BMP, TIFF, WEBP, HEIC, HEIF, GIF |

**Example triggers / 触发示例：**

- "Parse this PDF for me" / "帮我解析这个 PDF"
- "Extract the key clauses from this contract" / "提取合同中的关键条款"
- "Summarize the paper I just uploaded" / "总结这篇论文的主要内容"
- "Convert this document to Markdown" / "把这个文档转成 Markdown"
- "What does this image say?" / "这张图片里写了什么"

---

## Setup / 配置

Get an API key at [somark.tech](https://somark.tech), then set it as an environment variable. Use the command for your terminal:

前往 [somark.tech](https://somark.tech) 获取 API Key，然后设置环境变量：

**macOS / Linux（Bash、Zsh）**

```bash
# macOS / Linux (Bash, Zsh)
export SOMARK_API_KEY="sk-your-api-key"
```

**Linux（Fish）**

```fish
set -x SOMARK_API_KEY "sk-your-api-key"
```

**Windows PowerShell**

```powershell
$env:SOMARK_API_KEY = "sk-your-api-key"
```

**Windows Command Prompt（CMD）**

```bat
set "SOMARK_API_KEY=sk-your-api-key"
```

Windows PowerShell users can persist the setting with `[Environment]::SetEnvironmentVariable("SOMARK_API_KEY", "sk-your-api-key", "User")`; open a new terminal afterwards. On Windows, run the parser with a quoted Windows path, for example `python .\somark_parser.py -f "C:\Users\your-name\Documents\file.pdf" -o ".\output"`. Do not use Unix `export`, `/path/...`, or Bash backslash line continuations in PowerShell or CMD.

Or add it to your agent's settings. The skill will guide you through setup on first use.

也可以在 agent 设置中配置。首次使用时，skill 会自动引导你完成配置。

**Free quota / 免费额度：** SoMark offers a free tier — visit the [purchase page](https://somark.tech/workbench/purchase) to claim it.

SoMark 提供免费解析额度，访问[购买页面](https://somark.tech/workbench/purchase)扫描企业微信二维码即可领取。

---

## Why SoMark / 为什么选择 SoMark

Most agents struggle with documents because raw PDF/image data loses structure. SoMark preserves:

大多数 AI 助手处理文档时效果不理想，因为原始 PDF/图片数据会丢失结构信息。SoMark 完整保留：

- **Heading hierarchy / 标题层级** — agents understand document sections / AI 能准确理解文档章节
- **Tables / 表格** — fully reconstructed, not flattened into prose / 完整还原，不变成散乱文字
- **Formulas and diagrams / 公式与图表** — converted to LaTeX or described accurately / 转为 LaTeX 或精准描述
- **Multi-column layouts / 多栏排版** — reading order maintained / 阅读顺序完整保留

The result: your agent gives accurate, context-aware answers instead of hallucinating from garbled text.

效果：AI 给出准确、有据可查的回答，而不是从乱码文本中"脑补"。

---

## Limits / 使用限制

| Constraint / 限制项 | Limit / 上限 |
|--------------------|-------------|
| Max file size / 单文件大小 | 200 MB |
| Max pages / 单文件页数 | 300 页 |
| QPS per account / 账号 QPS | 4 |

---

## License

MIT
