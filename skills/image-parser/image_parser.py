import argparse
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SUPPORTED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
    ".gif",
}
SOMARK_SYNC_URL = "https://somark.tech/api/v1/parse/sync"


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

SUPPORTED_FEATURE_CONFIGS = {
    "enable_text_cross_page": False,
    "enable_table_cross_page": False,
    "enable_title_level_recognition": False,
    "enable_inline_image": True,
    "enable_table_image": True,
    "enable_image_understanding": True,
    "keep_header_footer": False,
}


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
        description="Image Parser: use SoMark sync API to parse image text and normalize to text+bbox schema"
    )
    parser.add_argument("-f", "--file", type=str, help="single image file path")
    parser.add_argument("-d", "--dir", type=str, help="image directory path")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="./image_parser_output",
        help="output directory",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help="SoMark API key [NOT RECOMMENDED: exposes key in process list and shell history; prefer SOMARK_API_KEY env var]",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="number of retries on network error (default: 2)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="request timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--include-without-bbox",
        action="store_true",
        help="include text items even if bbox is not found",
    )

    parser.add_argument(
        "--element-formats",
        type=parse_json_dict,
        default={},
        help='element formats as a JSON object, for example \'{"image": "url", "formula": "latex", "table": "html"}\'',
    )
    parser.add_argument(
        "--feature-config",
        type=parse_json_dict,
        default={},
        help='feature config as a JSON object, for example \'{"enable_inline_image": true, "enable_table_image": true}\'',
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        help="also save SoMark outputs.json as *.json",
    )
    parser.add_argument(
        "--save-response",
        action="store_true",
        help="also save raw SoMark response as *.somark.response.json",
    )
    parser.add_argument(
        "--save-legacy-parsed",
        action="store_true",
        help="also save legacy *.parsed.json (same content as *.text_bbox.json)",
    )
    return parser.parse_args()


def resolve_input_and_images(args: argparse.Namespace) -> tuple[Path, list[Path]]:
    input_value = args.file or args.dir
    if not input_value:
        raise ValueError("请通过 -f 或 -d 指定输入路径")

    input_path = Path(input_value).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"输入路径不存在: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(f"不支持的图片格式: {input_path.suffix}")
        return input_path, [input_path]

    images = [
        path
        for path in sorted(input_path.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]
    if not images:
        raise ValueError(f"目录中未找到支持的图片文件: {input_path}")
    return input_path, images


def resolve_api_key(args: argparse.Namespace) -> str:
    return args.api_key or os.environ.get("SOMARK_API_KEY") or ""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_multipart_data(
    file_path: Path,
    api_key: str,
    element_formats: dict[str, str],
    feature_config: dict[str, bool],
) -> tuple[str, bytes]:
    boundary = f"somark-{uuid.uuid4().hex}"
    body = bytearray()

    def add_text_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    add_text_field("output_formats", "json")
    add_text_field("output_formats", "markdown")
    add_text_field("output_formats", "somarkdown")

    add_text_field("api_key", api_key)
    add_text_field("element_formats", json.dumps(element_formats, ensure_ascii=False))
    add_text_field("feature_config", json.dumps(feature_config, ensure_ascii=False))

    mime_type, _ = mimetypes.guess_type(file_path.name)
    mime_type = mime_type or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    return boundary, bytes(body)


def call_somark_sync(
    file_path: Path,
    timeout: int,
    api_key: str,
    element_formats: dict[str, str],
    feature_config: dict[str, bool],
    retries: int = 0,
) -> dict[str, Any]:
    if not api_key:
        raise EnvironmentError("请先配置环境变量 SOMARK_API_KEY")

    for attempt in range(retries + 1):
        boundary, body = build_multipart_data(
            file_path=file_path,
            api_key=api_key,
            element_formats=element_formats,
            feature_config=feature_config,
        )
        req = urllib.request.Request(SOMARK_SYNC_URL, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"SoMark HTTP错误: {error.code} {error.reason}; body={detail}"
            ) from error
        except urllib.error.URLError as error:
            if attempt < retries:
                continue
            raise RuntimeError(f"SoMark 请求失败: {error.reason}") from error

        try:
            return json.loads(payload)
        except json.JSONDecodeError as error:
            raise RuntimeError("SoMark 响应不是合法 JSON") from error

    raise RuntimeError("SoMark 请求失败: 超出重试次数")


def normalize_bbox(raw_bbox: Any) -> list[float] | None:
    # Coordinates are pixel values in [x1, y1, x2, y2] format (top-left to bottom-right)
    # as returned by SoMark API. Unit: pixels on the original image.
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        return None
    if not all(is_number(value) for value in raw_bbox):
        return None

    x1, y1, x2, y2 = [float(value) for value in raw_bbox]
    if x2 < x1 or y2 < y1:
        return None
    return [x1, y1, x2, y2]


def extract_text_bbox_items(
    raw_json: dict[str, Any], include_without_bbox: bool
) -> tuple[list[dict[str, Any]], int]:
    pages = raw_json.get("pages")
    if not isinstance(pages, list):
        raise RuntimeError("SoMark 返回结果中 pages 结构无效")

    items: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_num = page.get("page_num")
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            continue

        for block in blocks:
            if not isinstance(block, dict):
                continue
            content = block.get("content")
            if not isinstance(content, str):
                continue

            text = content.strip()
            if not text:
                continue

            bbox = normalize_bbox(block.get("bbox"))
            if bbox is None and not include_without_bbox:
                continue

            items.append(
                {
                    "text": text,
                    "bbox": bbox,
                    "page": page_num if isinstance(page_num, int) else None,
                    "role": (
                        block.get("type")
                        if isinstance(block.get("type"), str)
                        else "unknown"
                    ),
                }
            )

    return dedupe_items(items), len(pages)


def build_outputs(
    response: dict[str, Any],
    image_path: Path,
    include_without_bbox: bool,
) -> tuple[dict[str, Any], dict[str, Any], str | None, str | None]:
    code = response.get("code")
    message = response.get("message", "")
    if code not in (0, 200):
        raise RuntimeError(f"SoMark 返回错误: code={code}, message={message}")

    data = response.get("data") or {}
    result = data.get("result") or {}
    outputs = result.get("outputs") or {}

    raw_json = outputs.get("json")
    markdown = outputs.get("markdown")
    somarkdown = outputs.get("somarkdown")
    if not isinstance(raw_json, dict):
        raise RuntimeError("SoMark 返回结果缺少 outputs.json")

    items, page_count = extract_text_bbox_items(
        raw_json=raw_json, include_without_bbox=include_without_bbox
    )

    text_bbox = {
        "image": str(image_path),
        "file_name": result.get("file_name") or image_path.name,
        "task_id": data.get("task_id"),
        "items": items,
        "stats": {
            "page_count": page_count,
            "item_count": len(items),
            "with_bbox": sum(1 for item in items if item.get("bbox") is not None),
        },
    }
    return (
        raw_json,
        text_bbox,
        markdown if isinstance(markdown, str) else None,
        somarkdown if isinstance(somarkdown, str) else None,
    )


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        bbox = item.get("bbox")
        bbox_key = tuple(bbox) if isinstance(bbox, list) else None
        key = (item.get("text"), bbox_key, item.get("page"), item.get("role"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def main() -> None:
    args = parse_args()
    input_path, images = resolve_input_and_images(args)
    api_key = resolve_api_key(args)

    element_formats = DEFAULT_ELEMENT_FORMATS.copy()
    element_formats.update(args.element_formats)
    for key, value in element_formats.items():
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

    feature_config = SUPPORTED_FEATURE_CONFIGS.copy()
    feature_config.update(args.feature_config)
    for key, value in feature_config.items():
        if key not in SUPPORTED_FEATURE_CONFIGS:
            supported = ", ".join(SUPPORTED_FEATURE_CONFIGS.keys())
            raise ValueError(f"不支持的功能配置: {key}，仅支持: {supported}")
        if not isinstance(value, bool):
            raise ValueError(
                f"功能配置 {key} 的值必须是布尔值，当前值: {value}, 类型: {type(value)}"
            )

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, Any]] = []
    for image_path in images:
        response = call_somark_sync(
            file_path=image_path,
            timeout=args.timeout,
            api_key=api_key,
            element_formats=element_formats,
            feature_config=feature_config,
            retries=args.retries,
        )

        raw_json, text_bbox, markdown, somarkdown = build_outputs(
            response=response,
            image_path=image_path,
            include_without_bbox=args.include_without_bbox,
        )

        text_bbox_path = output_dir / f"{image_path.stem}.text_bbox.json"
        text_bbox_path.write_text(
            json.dumps(text_bbox, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        markdown_path: Path | None = None
        if markdown:
            markdown_path = output_dir / f"{image_path.stem}.md"
            markdown_path.write_text(markdown, encoding="utf-8")

        somarkdown_path: Path | None = None
        if somarkdown:
            somarkdown_path = output_dir / f"{image_path.stem}-smd.md"
            somarkdown_path.write_text(somarkdown, encoding="utf-8")

        json_path: Path | None = None
        if args.save_json:
            json_path = output_dir / f"{image_path.stem}.json"
            json_path.write_text(
                json.dumps(raw_json, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        response_path: Path | None = None
        if args.save_response:
            response_path = output_dir / f"{image_path.stem}.somark.response.json"
            response_path.write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        parsed_path: Path | None = None
        if args.save_legacy_parsed:
            parsed_path = output_dir / f"{image_path.stem}.parsed.json"
            parsed_path.write_text(
                json.dumps(text_bbox, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        entry: dict[str, Any] = {
            "image": str(image_path),
            "text_bbox": str(text_bbox_path),
            "markdown": str(markdown_path) if markdown_path else None,
            "item_count": text_bbox["stats"]["item_count"],
            "page_count": text_bbox["stats"]["page_count"],
        }
        if somarkdown_path:
            entry["somarkdown"] = str(somarkdown_path)
        if json_path:
            entry["json"] = str(json_path)
        if response_path:
            entry["response"] = str(response_path)
        if parsed_path:
            entry["parsed"] = str(parsed_path)
        summary.append(entry)

    index = {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "results": summary,
    }
    index_path = output_dir / "results_index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"完成：共处理 {len(summary)} 个文件（同步解析）")
    print(f"结果索引：{index_path}")


if __name__ == "__main__":
    main()
