import argparse
import asyncio
import difflib
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

SOMARK_BASE = "https://somark.tech/api/v1"

ASYNC_URL = f"{SOMARK_BASE}/parse/async"
CHECK_URL = f"{SOMARK_BASE}/parse/async_check"


SUPPORTED_OUTPUT_FORMATS = {"markdown", "json"}


SUPPORTED_FORMATS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
    ".gif",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
}

SUPPORTED_ELEMENT_FORMATS = {
    "image": ["url", "base64", "none"],
    "formula": ["latex", "mathml", "ascii"],
    "table": ["html", "image", "markdown"],
    "cs": ["image"],
}

DEFAULT_FEATURE_CONFIGS = {
    "enable_text_cross_page": False,
    "enable_table_cross_page": False,
    "enable_title_level_recognition": False,
    "enable_inline_image": True,
    "enable_table_image": True,
    "enable_image_understanding": True,
    "keep_header_footer": False,
}

REQUEST_LOCK = asyncio.Lock()


async def post_json(
    session: aiohttp.ClientSession,
    url: str,
    **kwargs: Any,
) -> tuple[int, Any]:
    async with REQUEST_LOCK:
        async with session.post(url, **kwargs) as resp:
            status = resp.status
            if status != 200:
                return status, await resp.text()
            return status, await resp.json()


def parse_json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)

    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"数组参数必须是合法 JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError(
            '数组参数必须是 JSON 数组，例如 \'["markdown", "json"]\''
        )

    normalized: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            raise argparse.ArgumentTypeError("数组参数中的每一项都必须是非空字符串")

        normalized.append(item.strip())

    return normalized



def parse_json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"字典参数必须是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError(
            '字典参数必须是 JSON 对象，例如 \'{"image": "url"}\''
        )

    for key, value in parsed.items():
        if isinstance(value, str) and not value.strip():
            raise argparse.ArgumentTypeError(f"字典参数中的字段 '{key}' 不能为空字符串")
        elif isinstance(value, str) and value.strip():
            parsed[key] = value.strip()

    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SoMark 文档对比工具：解析两份文档并生成差异报告"
    )
    parser.add_argument(
        "-f1", "--file1", required=True, type=str, help="第一份文档路径（原始版本）"
    )
    parser.add_argument(
        "-f2", "--file2", required=True, type=str, help="第二份文档路径（新版本）"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="./document_diff_output",
        help="输出目录（默认：./document_diff_output）",
    )

    parser.add_argument(
        "--output-formats",
        type=parse_json_list,
        default=["markdown", "json"],
        help='输出格式，传 JSON 数组，例如 \'["markdown", "json"]\'',
    )

    parser.add_argument(
        "--element-formats",
        type=parse_json_dict,
        default={"image": "url", "formula": "latex", "table": "html", "cs": "image"},
        help='元素格式，传 JSON 对象，例如 \'{"image": "url", "formula": "latex", "table": "html"}\'',
    )

    parser.add_argument(
        "--feature-config",
        type=parse_json_dict,
        default={
            "enable_text_cross_page": False,
            "enable_table_cross_page": False,
            "enable_title_level_recognition": False,
            "enable_inline_image": True,
            "enable_table_image": True,
            "enable_image_understanding": True,
            "keep_header_footer": False,
        },
        help='功能配置，传 JSON 对象，例如 \'{"enable_text_cross_page": false, "enable_table_cross_page": false, "enable_title_level_recognition": false, "enable_inline_image": true, "enable_table_image": true, "enable_image_understanding": true, "keep_header_footer": false}\'',
    )

    return parser.parse_args()


def resolve_file(path_str: str) -> Path:
    path = Path(path_str).resolve()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"路径不是文件: {path}")
    if path.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的文件格式: {path.suffix}")
    return path


async def submit_task(
    session: aiohttp.ClientSession,
    file_path: Path,
    api_key: str,
    output_formats: list[str],
    element_formats: dict,
    feature_config: dict,
) -> str:
    data = aiohttp.FormData()

    data.add_field("api_key", api_key)
    data.add_field("file", file_path.read_bytes(), filename=file_path.name)
    data.add_field("element_formats", json.dumps(element_formats, ensure_ascii=False))
    data.add_field("feature_config", json.dumps(feature_config, ensure_ascii=False))

    for output_format in output_formats:
        data.add_field("output_formats", output_format)

    status, body = await post_json(session, ASYNC_URL, data=data)
    if status != 200:
        raise RuntimeError(f"提交任务失败 [{status}]: {body}")

    task_id = (body.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"响应中缺少 task_id: {body}")
    return task_id


async def poll_task(
    session: aiohttp.ClientSession,
    task_id: str,
    api_key: str,
    max_retries: int = 300,
    interval: int = 2,
) -> dict:
    for _ in range(max_retries):
        await asyncio.sleep(interval)
        status, body = await post_json(
            session, CHECK_URL, data={"api_key": api_key, "task_id": task_id}
        )
        if status != 200:
            continue

        data = body.get("data") or {}
        status = data.get("status")
        if status == "FAILED":
            raise RuntimeError(f"SoMark 任务失败: {data}")
        if status == "SUCCESS":
            result = data.get("result") or {}
            return result.get("outputs") or result

    raise RuntimeError(f"任务轮询超时: task_id={task_id}")


async def parse_document(
    session: aiohttp.ClientSession,
    file_path: Path,
    api_key: str,
    output_formats: list[str],
    element_formats: dict,
    feature_config: dict,
) -> dict:
    print(f"  提交解析: {file_path.name}")
    task_id = await submit_task(
        session, file_path, api_key, output_formats, element_formats, feature_config
    )
    print(f"  等待结果 (task_id={task_id})...")
    outputs = await poll_task(session, task_id, api_key)
    print(f"  解析完成: {file_path.name}")
    return outputs


def extract_markdown(outputs: dict, file_path: Path) -> str:
    md = outputs.get("markdown")
    if isinstance(md, str) and md.strip():
        return md

    # 降级：从 json outputs 中提取纯文本
    json_data = outputs.get("json")
    if isinstance(json_data, dict):
        lines = []
        for page in json_data.get("pages") or []:
            for block in page.get("blocks") or []:
                content = block.get("content", "").strip()
                if content:
                    lines.append(content)
        return "\n".join(lines)
    return ""


def build_diff_report(file1: Path, file2: Path, md1: str, md2: str) -> str:
    lines1 = md1.splitlines(keepends=True)
    lines2 = md2.splitlines(keepends=True)

    diff = list(
        difflib.unified_diff(
            lines1,
            lines2,
            fromfile=file1.name,
            tofile=file2.name,
            lineterm="",
        )
    )

    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    unchanged = len([l for l in difflib.ndiff(lines1, lines2) if l.startswith("  ")])

    report_lines = [
        "# 文档对比报告",
        "",
        "## 概览",
        "",
        f"| 项目 | 内容 |",
        f"|------|------|",
        f"| 原始文件 | `{file1.name}` |",
        f"| 新版本文件 | `{file2.name}` |",
        f"| 新增行数 | {added} |",
        f"| 删除行数 | {removed} |",
        f"| 未变更行数 | {unchanged} |",
        "",
        "## 差异详情",
        "",
        "```diff",
    ]
    report_lines.extend(l.rstrip("\n") for l in diff)
    report_lines.append("```")
    report_lines.append("")

    return "\n".join(report_lines)


async def main() -> None:
    args = parse_args()
    api_key = os.environ.get("SOMARK_API_KEY", "")
    if not api_key:
        print("错误：请设置环境变量 SOMARK_API_KEY")
        raise SystemExit(1)

    file1 = resolve_file(args.file1)
    file2 = resolve_file(args.file2)

    

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    output_formats = args.output_formats

    for output_format in output_formats:
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            supported = ", ".join(SUPPORTED_OUTPUT_FORMATS)

            print(f"不支持的输出格式: {output_format}，仅支持: {supported}")
            raise SystemExit(1)

    element_formats = args.element_formats

    for k, v in element_formats.items():
        if k not in SUPPORTED_ELEMENT_FORMATS:
            print(
                f"不支持的元素格式: {k}，仅支持: {', '.join(SUPPORTED_ELEMENT_FORMATS.keys())}"
            )
            raise SystemExit(1)
        if v not in SUPPORTED_ELEMENT_FORMATS[k]:
            print(
                f"元素格式{k}不支持值: {v}，仅支持: {', '.join(SUPPORTED_ELEMENT_FORMATS[k])}"
            )
            raise SystemExit(1)

        if not isinstance(v, str):
            print(f"元素格式{k}的值必须是字符串，当前值: {v}, 类型: {type(v)}")
            raise SystemExit(1)

    feature_config = args.feature_config

    for k, v in feature_config.items():
        if k not in DEFAULT_FEATURE_CONFIGS:
            print(
                f"不支持的功能配置: {k}，仅支持: {', '.join(DEFAULT_FEATURE_CONFIGS.keys())}"
            )
            raise SystemExit(1)
        if not isinstance(v, bool):
            print(f"功能配置{k}的值必须是布尔值，当前值: {v}, 类型: {type(v)}")
            raise SystemExit(1)

    print(f"\n开始解析文档对...")
    start = time.time()

    connector = aiohttp.TCPConnector(limit=1, limit_per_host=1)
    async with aiohttp.ClientSession(connector=connector) as session:
        outputs1 = await parse_document(
            session, file1, api_key, output_formats, element_formats, feature_config
        )
        outputs2 = await parse_document(
            session, file2, api_key, output_formats, element_formats, feature_config
        )

    md1 = extract_markdown(outputs1, file1)
    md2 = extract_markdown(outputs2, file2)

    if not md1:
        print(f"警告：{file1.name} 解析内容为空")
    if not md2:
        print(f"警告：{file2.name} 解析内容为空")

    # 保存各自的 markdown
    (output_dir / f"{file1.stem}.md").write_text(md1, encoding="utf-8")
    (output_dir / f"{file2.stem}.md").write_text(md2, encoding="utf-8")

    # 生成差异报告
    report = build_diff_report(file1, file2, md1, md2)
    report_path = output_dir / "diff_report.md"
    report_path.write_text(report, encoding="utf-8")

    # 保存摘要 JSON
    summary = {
        "file1": str(file1),
        "file2": str(file2),
        "output_dir": str(output_dir),
        "report": str(report_path),
        "file1_markdown": str(output_dir / f"{file1.stem}.md"),
        "file2_markdown": str(output_dir / f"{file2.stem}.md"),
        "elapsed_seconds": round(time.time() - start, 2),
    }
    (output_dir / "diff_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n完成：耗时 {summary['elapsed_seconds']} 秒")
    print(f"差异报告：{report_path}")


if __name__ == "__main__":
    asyncio.run(main())
