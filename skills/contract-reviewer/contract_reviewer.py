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
    ".doc",
    ".docx",
}

# SUPPORTED_OUTPUT_FORMATS = {"markdown", "json", "somarkdown","zip"}
SUPPORTED_OUTPUT_FORMATS = {"markdown", "json", "somarkdown"}

# SUPPORTED_ELEMENT_FORMATS = {
#     "image": ["url", "base64", "file", "none"],
#     "formula": ["latex", "mathml", "ascii"],
#     "table": ["html", "image", "markdown"],
#     "cs": ["image"],
# }

SUPPORTED_ELEMENT_FORMATS = {
    "image": ["url", "base64", "none"],
    "formula": ["latex", "mathml", "ascii"],
    "table": ["html", "image", "markdown"],
    "cs": ["image"],
}

SUPPORTED_FEATURE_CONFIGS = {
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

    for key, value in parsed.items():
        if isinstance(value, str) and not value.strip():
            raise argparse.ArgumentTypeError(f"字典参数中的字段 '{key}' 不能为空字符串")
        elif isinstance(value, str) and value.strip():
            parsed[key] = value.strip()

    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SoMark 合同解析工具：将合同文件转换为结构化内容以供风险审查"
    )
    parser.add_argument("-f", "--file", required=True, type=str, help="合同文件路径")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="./contract_reviewer_output",
        help="输出目录（默认：./contract_reviewer_output）",
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
    for fmt in output_formats:
        data.add_field("output_formats", fmt)
    data.add_field("element_formats", json.dumps(element_formats, ensure_ascii=False))
    data.add_field("feature_config", json.dumps(feature_config, ensure_ascii=False))

    async with session.post(ASYNC_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise RuntimeError(f"提交任务失败 [{resp.status}]: {error_text}")
        body = await resp.json()

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
        async with session.post(
            CHECK_URL, data={"api_key": api_key, "task_id": task_id}
        ) as resp:
            if resp.status != 200:
                continue
            body = await resp.json()

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

    output_formats = args.output_formats

    for output_format in output_formats:
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            supported = ", ".join(SUPPORTED_OUTPUT_FORMATS)

            print(f"不支持的输出格式: {output_format}，仅支持: {supported}")
            raise SystemExit(1)

    # isZipOnly = (
    #     len(output_formats) == 1 and output_formats[0] == "zip"
    # )  # 是否只有zip格式
    # isZipWithJson = (
    #     len(output_formats) == 2
    #     and "json" in output_formats
    #     and "zip" in output_formats
    # )  # 是否同时包含json格式和zip格式

    # if not isZipOnly or not isZipWithJson:
    #     print(
    #         "错误：output-formats参数如果含有zip格式，必须同时包含json格式或者只有zip格式一个"
    #     )
    #     raise SystemExit(1)

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

        # if k not in SUPPORTED_ELEMENT_FORMATS.keys():
        #     if "zip" in output_formats:
        #         element_formats[k] = "file"
        #     else:
        #         element_formats[k] = "url"

    feature_config = args.feature_config

    for k, v in feature_config.items():
        if k not in SUPPORTED_FEATURE_CONFIGS:
            print(
                f"不支持的功能配置: {k}，仅支持: {', '.join(SUPPORTED_FEATURE_CONFIGS.keys())}"
            )
            raise SystemExit(1)
        if not isinstance(v, bool):
            print(f"功能配置{k}的值必须是布尔值，当前值: {v}, 类型: {type(v)}")
            raise SystemExit(1)

    print(f"\n开始解析合同: {file_path.name}")
    start = time.time()

    async with aiohttp.ClientSession() as session:
        print("  提交解析任务...")
        task_id = await submit_task(
            session, file_path, api_key, output_formats, element_formats, feature_config
        )
        print(f"  等待结果 (task_id={task_id})...")
        outputs = await poll_task(session, task_id, api_key)

    elapsed = round(time.time() - start, 2)

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
        print(f"  SomarkDown 已保存: {somarkdown_path}")

    summary = {
        "file": str(file_path),
        "output_dir": str(output_dir),
        "markdown": str(md_path) if md_content else None,
        "json": str(json_path) if json_content else None,
        "somarkdown": str(somarkdown_path) if somarkdown_content else None,
        "elapsed_seconds": elapsed,
    }
    (output_dir / "parse_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n完成：耗时 {elapsed} 秒")
    print(f"输出目录：{output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
