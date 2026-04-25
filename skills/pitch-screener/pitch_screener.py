import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

SOMARK_BASE = "https://somark.tech/api/v1"

ASYNC_URL = f"{SOMARK_BASE}/parse/async"
CHECK_URL = f"{SOMARK_BASE}/parse/async_check"

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
    ".ppt",
    ".pptx",
}

SUPPORTED_OUTPUT_FORMATS = {"markdown", "json"}

SUPPORTED_ELEMENT_FORMATS = {
    "image": ["url", "base64", "none"],
    "formula": ["latex", "mathml", "ascii"],
    "table": ["html", "image", "markdown"],
    "cs": ["image"],
}

DEFAULT_ELEMENT_FORMATS = {
    "image": "url",
    "formula": "latex",
    "table": "html",
    "cs": "image",
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

    for key, item in parsed.items():
        if isinstance(item, str) and not item.strip():
            raise argparse.ArgumentTypeError(f"字典参数中的字段 '{key}' 不能为空字符串")
        if isinstance(item, str):
            parsed[key] = item.strip()

    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SoMark Pitch Screener：解析融资演示文稿，生成投资备忘录所需的结构化内容"
    )
    parser.add_argument(
        "-f", "--file", required=True, type=str, help="Pitch deck 文件路径"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="./pitch_screener_output",
        help="输出目录（默认：./pitch_screener_output）",
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
        default=DEFAULT_ELEMENT_FORMATS,
        help='元素格式，传 JSON 对象，例如 \'{"image": "url", "formula": "latex", "table": "html"}\'',
    )
    parser.add_argument(
        "--feature-config",
        type=parse_json_dict,
        default=DEFAULT_FEATURE_CONFIGS,
        help='功能配置，传 JSON 对象，例如 \'{"enable_inline_image": true, "enable_table_image": true}\'',
    )
    return parser.parse_args()


async def submit_task(
    session: aiohttp.ClientSession,
    file_path: Path,
    api_key: str,
    output_formats: list[str],
    element_formats: dict[str, str],
    feature_config: dict[str, bool],
) -> str:
    data = aiohttp.FormData()
    data.add_field("api_key", api_key)
    data.add_field("file", file_path.read_bytes(), filename=file_path.name)
    for output_format in output_formats:
        data.add_field("output_formats", output_format)
    data.add_field("element_formats", json.dumps(element_formats, ensure_ascii=False))
    data.add_field("feature_config", json.dumps(feature_config, ensure_ascii=False))

    status, body = await post_json(session, ASYNC_URL, data=data)
    if status != 200:
        raise RuntimeError(f"提交任务失败 [{status}]: {body}")

    task_id = (body.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"响应中缺少 task_id: {body}")
    return task_id


async def poll_task(session: aiohttp.ClientSession, task_id: str, api_key: str,
                    max_retries: int = 300, interval: int = 2) -> dict:
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


async def main() -> None:
    args = parse_args()
    api_key = os.environ.get("SOMARK_API_KEY", "")
    if not api_key:
        print("错误：请设置环境变量 SOMARK_API_KEY")
        raise SystemExit(1)

    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"错误：文件不存在: {file_path}")
        raise SystemExit(1)
    if file_path.suffix.lower() not in SUPPORTED_FORMATS:
        print(f"错误：不支持的文件格式: {file_path.suffix}")
        raise SystemExit(1)

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    output_formats = [output_format.strip() for output_format in args.output_formats]
    for output_format in output_formats:
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_OUTPUT_FORMATS))
            print(f"错误：不支持的输出格式: {output_format}，仅支持: {supported}")
            raise SystemExit(1)

    element_formats = DEFAULT_ELEMENT_FORMATS.copy()
    element_formats.update(args.element_formats)
    for key, value in element_formats.items():
        if key not in SUPPORTED_ELEMENT_FORMATS:
            supported = ", ".join(SUPPORTED_ELEMENT_FORMATS.keys())
            print(f"错误：不支持的元素格式: {key}，仅支持: {supported}")
            raise SystemExit(1)
        if not isinstance(value, str):
            print(
                f"错误：元素格式 {key} 的值必须是字符串，当前值: {value}, 类型: {type(value)}"
            )
            raise SystemExit(1)
        if value not in SUPPORTED_ELEMENT_FORMATS[key]:
            supported = ", ".join(SUPPORTED_ELEMENT_FORMATS[key])
            print(f"错误：元素格式 {key} 不支持值: {value}，仅支持: {supported}")
            raise SystemExit(1)

    feature_config = DEFAULT_FEATURE_CONFIGS.copy()
    feature_config.update(args.feature_config)
    for key, value in feature_config.items():
        if key not in DEFAULT_FEATURE_CONFIGS:
            supported = ", ".join(DEFAULT_FEATURE_CONFIGS.keys())
            print(f"错误：不支持的功能配置: {key}，仅支持: {supported}")
            raise SystemExit(1)
        if not isinstance(value, bool):
            print(
                f"错误：功能配置 {key} 的值必须是布尔值，当前值: {value}, 类型: {type(value)}"
            )
            raise SystemExit(1)

    print(f"\n开始解析 Pitch Deck: {file_path.name}")
    start = time.time()

    connector = aiohttp.TCPConnector(limit=1, limit_per_host=1)
    async with aiohttp.ClientSession(connector=connector) as session:
        print("  提交解析任务...")
        task_id = await submit_task(
            session,
            file_path,
            api_key,
            output_formats,
            element_formats,
            feature_config,
        )
        print(f"  等待结果 (task_id={task_id})...")
        outputs = await poll_task(session, task_id, api_key)

    elapsed = round(time.time() - start, 2)

    md_content = outputs.get("markdown", "")
    json_content = outputs.get("json", {})



    md_path = output_dir / f"{file_path.stem}.md"
    json_path = output_dir / f"{file_path.stem}.json"


    if md_content:
        md_path.write_text(md_content, encoding="utf-8")
        print(f"  Markdown 已保存: {md_path}")

    if json_content:
        json_path.write_text(json.dumps(json_content, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON 已保存: {json_path}")

 

    summary = {
        "file": str(file_path),
        "output_dir": str(output_dir),
        "markdown": str(md_path) if md_content else None,
        "json": str(json_path) if json_content else None,
    
        "elapsed_seconds": elapsed,
    }
    (output_dir / "parse_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n完成：耗时 {elapsed} 秒")
    print(f"输出目录：{output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
