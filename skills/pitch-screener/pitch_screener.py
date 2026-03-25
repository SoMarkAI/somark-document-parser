import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

SOMARK_BASE = "https://somark.tech/api/v1"
ASYNC_URL = f"{SOMARK_BASE}/extract/async"
CHECK_URL = f"{SOMARK_BASE}/extract/async_check"

SUPPORTED_FORMATS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".heic", ".heif", ".gif",
    ".ppt", ".pptx",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SoMark Pitch Screener：解析融资演示文稿，生成投资备忘录所需的结构化内容")
    parser.add_argument("-f", "--file", required=True, type=str, help="Pitch deck 文件路径")
    parser.add_argument("-o", "--output", type=str, default="./pitch_screener_output", help="输出目录（默认：./pitch_screener_output）")
    return parser.parse_args()


async def submit_task(session: aiohttp.ClientSession, file_path: Path, api_key: str) -> str:
    data = aiohttp.FormData()
    data.add_field("output_formats", "markdown")
    data.add_field("output_formats", "json")
    data.add_field("api_key", api_key)
    data.add_field("file", file_path.read_bytes(), filename=file_path.name)

    async with session.post(ASYNC_URL, data=data) as resp:
        if resp.status != 200:
            error_text = await resp.text()
            raise RuntimeError(f"提交任务失败 [{resp.status}]: {error_text}")
        body = await resp.json()

    task_id = (body.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"响应中缺少 task_id: {body}")
    return task_id


async def poll_task(session: aiohttp.ClientSession, task_id: str, api_key: str,
                    max_retries: int = 300, interval: int = 2) -> dict:
    for _ in range(max_retries):
        await asyncio.sleep(interval)
        async with session.post(CHECK_URL, data={"api_key": api_key, "task_id": task_id}) as resp:
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

    print(f"\n开始解析 Pitch Deck: {file_path.name}")
    start = time.time()

    async with aiohttp.ClientSession() as session:
        print("  提交解析任务...")
        task_id = await submit_task(session, file_path, api_key)
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
