# somark-document-parser

> [SoMark](https://somark.ai) 官方 Skills 合集，涵盖文档解析、图片 OCR 及智能提取 —— 专为 AI Agent 工作流设计。

## 安装

```bash
npx skills add https://github.com/SoMarkAI/somark-document-parser
```

兼容 Claude Code、Cursor、Cline、OpenCode 及 [40+ 其他 Agent](https://skills.sh)。

---

## Skills 列表

| Skill | 说明 |
|-------|------|
| **somark-document-parser** | 将 PDF、Word、PowerPoint、图片解析为结构化 Markdown 或 JSON |
| **image-parser** | 从图片中提取文本及精确坐标（OCR + 位置感知） |

---

## 功能说明

当你把文档交给 AI Agent 时，SoMark 会将文档解析为结构化 Markdown 或 JSON，Agent 能基于结构进行准确理解，而不只是处理 OCR 后的纯文本。

image-parser skill 进一步提供每个文本块在原图上的像素坐标，支持字段提取、区域定位和文档自动化等场景。

**支持格式：**

| 类型 | 格式 |
|------|------|
| 文档 | PDF, DOC, DOCX, PPT, PPTX |
| 图片 | PNG, JPG, JPEG, BMP, TIFF, WEBP, HEIC, HEIF, GIF |

**触发示例：**

- “帮我解析这个 PDF”
- “提取这份合同的关键条款”
- “总结我刚上传的论文”
- “把这个文档转成 Markdown”
- “这张图片里写了什么？”
- “提取这张图片中所有文字及坐标”
- “找出发票上的金额以及它在图片中的位置”

---

## 配置

先在 [somark.tech](https://somark.tech) 获取 API Key，然后设置环境变量：

```bash
export SOMARK_API_KEY=sk-your-api-key
```

也可以在 Agent 设置中配置。首次使用时，Skill 会引导你完成设置。

**免费额度：** SoMark 提供免费解析额度。可前往 [购买页面](https://somark.tech/workbench/purchase) 按页面说明领取。

---

## 为什么选择 SoMark

多数 Agent 处理文档效果不理想，是因为原始 PDF/图片数据会丢失结构。SoMark 会保留：

- **标题层级** —— Agent 能正确理解章节结构
- **表格** —— 完整还原，而不是被压平成普通文本
- **公式与图表** —— 转为 LaTeX 或准确描述
- **多栏排版** —— 保持正确阅读顺序

效果是：Agent 基于清晰结构回答，更准确、更有上下文，而不是从乱码中“猜测”。

---

## 使用限制

| 限制项 | 上限 |
|--------|------|
| 单文件大小 | 200 MB |
| 单文件页数 | 300 页 |
| 账号 QPS | 1 |

---

## License

MIT
