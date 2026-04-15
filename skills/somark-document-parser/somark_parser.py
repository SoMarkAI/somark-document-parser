import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

# SOMARK_BASE = "https://somark.tech/api/v1"
SOMARK_BASE = "https://somark-sit.soulcode.cn/api/v2"
ASYNC_URL = f"{SOMARK_BASE}/parse/async"
CHECK_URL = f"{SOMARK_BASE}/parse/async_check"

SUPPORTED_FORMATS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".jp2",
    ".dib",
    ".ppm",
    ".pgm",
    ".pbm",
    ".gif",
    ".heic",
    ".heif",
    ".webp",
    ".xpm",
    ".tga",
    ".dds",
    ".xbm",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
}

SUPPORTED_OUTPUT_FORMATS = {"markdown", "json", "somarkdown"}

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
    parser = argparse.ArgumentParser(description="SoMark 文档解析工具")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("-f", "--file", type=str, help="要解析的文件路径")
    input_group.add_argument("-d", "--dir", type=str, help="要解析的文件夹路径")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=".",
        help="输出目录（默认：当前目录）",
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


def resolve_input_paths(args: argparse.Namespace) -> tuple[Path, list[Path]]:
    input_path = Path(args.file or args.dir).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"路径不存在: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_FORMATS:
            raise ValueError(f"不支持的文件格式: {input_path.suffix}")
        return input_path, [input_path]

    files_list = sorted(
        [
            path
            for path in input_path.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_FORMATS
        ],
        key=lambda path: path.name,
    )
    if not files_list:
        raise ValueError(f"未找到支持的文件: {input_path}")
    return input_path, files_list


def normalize_output_formats(output_formats: list[str]) -> list[str]:
    normalized = [output_format.strip() for output_format in output_formats]
    for output_format in normalized:
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_OUTPUT_FORMATS))
            raise ValueError(f"不支持的输出格式: {output_format}，仅支持: {supported}")
    return normalized


def normalize_element_formats(element_formats: dict[str, Any]) -> dict[str, str]:
    normalized = DEFAULT_ELEMENT_FORMATS.copy()
    normalized.update(element_formats)

    for key, value in normalized.items():
        if key not in SUPPORTED_ELEMENT_FORMATS:
            supported = ", ".join(SUPPORTED_ELEMENT_FORMATS.keys())
            raise ValueError(f"不支持的元素格式: {key}，仅支持: {supported}")
        if not isinstance(value, str):
            raise ValueError(
                f"元素格式 {key} 的值必须是字符串，当前值: {value}, 类型: {type(value)}"
            )
        if value not in SUPPORTED_ELEMENT_FORMATS[key]:
            supported = ", ".join(SUPPORTED_ELEMENT_FORMATS[key])
            raise ValueError(f"元素格式 {key} 不支持值: {value}，仅支持: {supported}")

    return normalized


def normalize_feature_config(feature_config: dict[str, Any]) -> dict[str, bool]:
    normalized = DEFAULT_FEATURE_CONFIGS.copy()
    normalized.update(feature_config)

    for key, value in normalized.items():
        if key not in DEFAULT_FEATURE_CONFIGS:
            supported = ", ".join(DEFAULT_FEATURE_CONFIGS.keys())
            raise ValueError(f"不支持的功能配置: {key}，仅支持: {supported}")
        if not isinstance(value, bool):
            raise ValueError(
                f"功能配置 {key} 的值必须是布尔值，当前值: {value}, 类型: {type(value)}"
            )

    return normalized


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

    async with session.post(ASYNC_URL, data=data) as response:
        if response.status != 200:
            error_text = await response.text()
            raise RuntimeError(f"提交任务失败 [{response.status}]: {error_text}")
        body = await response.json()

    task_id = (body.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"响应中缺少 task_id: {body}")
    return task_id


async def poll_task(
    session: aiohttp.ClientSession,
    task_id: str,
    api_key: str,
    max_retries: int = 1000,
    interval: int = 2,
) -> dict[str, Any]:
    for _ in range(max_retries):
        await asyncio.sleep(interval)
        async with session.post(
            CHECK_URL, data={"api_key": api_key, "task_id": task_id}
        ) as response:
            if response.status != 200:
                continue
            body = await response.json()

        data = body.get("data") or {}
        status = data.get("status")
        if status == "FAILED":
            raise RuntimeError(f"SoMark 任务失败: {data}")
        if status == "SUCCESS":
            result = data.get("result") or {}
            return result.get("outputs") or result

    raise RuntimeError(f"任务轮询超时: task_id={task_id}")


def extract_metadata(outputs: dict[str, Any]) -> tuple[int, int]:
    json_data = outputs.get("json")
    if not isinstance(json_data, dict):
        return 0, 0

    metadata = json_data.get("metadata")
    if not isinstance(metadata, dict):
        return 0, 0

    page_count = metadata.get("page_count", 0)
    token_count = metadata.get("token_count", 0)
    return (
        page_count if isinstance(page_count, int) else 0,
        token_count if isinstance(token_count, int) else 0,
    )


def save_outputs(
    output_dir: Path, file_path: Path, outputs: dict[str, Any]
) -> dict[str, Any]:
    md_content = outputs.get("markdown", "")
    json_content = outputs.get("json", {})
    somarkdown_content = outputs.get("somarkdown", "")

    md_path = output_dir / f"{file_path.stem}.md"
    json_path = output_dir / f"{file_path.stem}.json"
    somarkdown_path = output_dir / f"{file_path.stem}-smd.md"

    if md_content:
        md_path.write_text(md_content, encoding="utf-8")
        print(f"  Markdown 已保存: {md_path}")

    if json_content:
        json_path.write_text(
            json.dumps(json_content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  JSON 已保存: {json_path}")

    if somarkdown_content:
        somarkdown_path.write_text(somarkdown_content, encoding="utf-8")
        print(f"  SoMarkdown 已保存: {somarkdown_path}")

    page_count, token_count = extract_metadata(outputs)
    return {
        "status": "success",
        "file": str(file_path),
        "markdown": str(md_path) if md_content else None,
        "json": str(json_path) if json_content else None,
        "somarkdown": str(somarkdown_path) if somarkdown_content else None,
        "page_count": page_count,
        "token_count": token_count,
    }


async def process_file_async(
    session: aiohttp.ClientSession,
    file_path: Path,
    api_key: str,
    output_dir: Path,
    output_formats: list[str],
    element_formats: dict[str, str],
    feature_config: dict[str, bool],
) -> dict[str, Any]:
    print(f"\n开始解析: {file_path.name}")
    start_time = time.time()

    try:
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

        elapsed = round(time.time() - start_time, 2)
        entry = save_outputs(output_dir, file_path, outputs)
        entry["elapsed_seconds"] = elapsed

        print(f"  页数: {entry['page_count']}")
        print(f"  Token 数: {entry['token_count']}")
        print(f"  耗时: {elapsed} 秒")
        return entry
    except Exception as exc:
        elapsed = round(time.time() - start_time, 2)
        print(f"  解析失败: {exc}")
        return {
            "status": "failed",
            "file": str(file_path),
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }


async def main() -> None:
    args = parse_args()
    api_key = os.environ.get("SOMARK_API_KEY", "")
    if not api_key:
        print("错误：请设置环境变量 SOMARK_API_KEY")
        print("用法: export SOMARK_API_KEY=your_key_here")
        raise SystemExit(1)

    try:
        input_path, files_list = resolve_input_paths(args)
        output_formats = normalize_output_formats(args.output_formats)
        element_formats = normalize_element_formats(args.element_formats)
        feature_config = normalize_feature_config(args.feature_config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}")
        raise SystemExit(1) from exc

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"找到 {len(files_list)} 个文件待处理")
    print(f"输出目录: {output_dir}")

    results: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for file_path in files_list:
            entry = await process_file_async(
                session,
                file_path,
                api_key,
                output_dir,
                output_formats,
                element_formats,
                feature_config,
            )
            results.append(entry)

    index = {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "request_options": {
            "output_formats": output_formats,
            "element_formats": element_formats,
            "feature_config": feature_config,
        },
        "results": results,
    }
    index_path = output_dir / "results_index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    success_count = sum(1 for entry in results if entry.get("status") == "success")
    failed_count = len(results) - success_count
    print(f"\n完成：成功 {success_count} 个，失败 {failed_count} 个")
    print(f"结果索引：{index_path}")

    if failed_count:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
