import aiohttp
import asyncio
from pathlib import Path
import json
import os
import argparse
import time

"""
使用方法：
1. 设置环境变量 SOMARK_API_KEY
2. 通过命令行参数指定要解析的文件或文件夹
3. 运行脚本
"""

def parse_args():
    parser = argparse.ArgumentParser(description='SoMark 文档解析工具')
    parser.add_argument('-f', '--file', type=str, help='要解析的文件路径')
    parser.add_argument('-d', '--dir', type=str, help='要解析的文件夹路径')
    parser.add_argument('-o', '--output', type=str, default='.', help='输出目录 (默认: 当前目录)')
    return parser.parse_args()

# 从环境变量获取 API Key（禁止通过命令行参数传入，避免暴露在进程列表中）
args = parse_args()
api_key = os.environ.get('SOMARK_API_KEY', '')

if not api_key:
    print("错误：请设置环境变量 SOMARK_API_KEY")
    print("用法: export SOMARK_API_KEY=your_key_here")
    print("     python somark_parser.py -f /path/to/file.pdf")
    exit(1)

# 确定输入文件
input_path = args.file or args.dir

if not input_path:
    print("错误：请指定文件或文件夹路径")
    print("用法: python somark_parser.py -f /path/to/file.pdf")
    print("   或: python somark_parser.py -d /path/to/folder")
    exit(1)

input_file_path = Path(input_path).resolve()

if not input_file_path.exists():
    print(f"错误：路径不存在: {input_file_path}")
    exit(1)

# 确定输出目录
output_dir = Path(args.output).resolve()
output_dir.mkdir(parents=True, exist_ok=True)

base_url = "https://somark.tech/api/v1"
async_url = f"{base_url}/extract/async"
check_url = f"{base_url}/extract/async_check"

SAVE_FILE = True

if input_file_path.is_file():
    files_list = [input_file_path]
else:
    supported_formats = {
        # PDF
        '.pdf',
        # 图片格式
        '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.jp2', '.dib',
        '.ppm', '.pgm', '.pbm', '.gif', '.heic', '.heif', '.webp', '.xpm',
        '.tga', '.dds', '.xbm',
        # Office文件格式
        '.doc', '.docx', '.ppt', '.pptx',
    }
    files_list = sorted(
        [p for p in input_file_path.iterdir() if p.suffix.lower() in supported_formats],
        key=lambda p: p.name
    )

if not files_list:
    print(f"未找到支持的文件: {input_file_path}")
    exit(1)

print(f"找到 {len(files_list)} 个文件待处理")
print(f"输出目录: {output_dir}")


async def check_task_status(session, api_key, task_id, max_retries=1000, retry_interval=2):
    """检查异步任务状态"""
    for attempt in range(max_retries):
        try:
            data = {
                'api_key': api_key,
                'task_id': task_id
            }
            async with session.post(check_url, data=data) as response:
                if response.status == 200:
                    resp = await response.json()
                    if not isinstance(resp, dict) or 'data' not in resp:
                        print(f"  Unexpected response structure, skipping")
                        await asyncio.sleep(retry_interval)
                        continue
                    data = resp['data']
                    if not isinstance(data, dict):
                        print(f"  Invalid data field in response, skipping")
                        await asyncio.sleep(retry_interval)
                        continue
                    print(f"  Check attempt {attempt + 1}: {str(data)[:100]}")
                    if 'status' in data and data['status'] == 'FAILED':
                        print(f"  Task failed: {str(data)[:200]}")
                        return None
                    if 'status' in data and data['status'] == 'SUCCESS':
                        result = data.get('result', {})
                        if 'outputs' in result:
                            return result['outputs']
                        else:
                            return result
                else:
                    print(f"  Check failed with status {response.status}")
        except Exception as e:
            print(f"  Check request failed: {e}")

        await asyncio.sleep(retry_interval)

    print(f"  Task check timeout after {max_retries} attempts")
    return None


async def process_file_async(session, file_path):
    """异步处理单个文件"""
    print(f"\n处理中: {file_path.name}")
    start_time = time.time()
    page_count = 0
    token_count = 0

    try:
        # 准备文件数据
        data = aiohttp.FormData()
        data.add_field('output_formats', 'markdown')
        data.add_field('output_formats', 'json')
        data.add_field('api_key', api_key)

        # 读取文件内容到内存中
        with open(file_path, 'rb') as f:
            file_content = f.read()

        # 添加文件到FormData
        data.add_field('file', file_content, filename=file_path.name)

        # 发送异步请求
        async with session.post(async_url, data=data) as response:
            print(f"  Status Code: {response.status}")
            if response.status != 200:
                error_text = await response.text()
                print(f"  Error response: {error_text}")
                return

            resp_json = await response.json()
            print(f"  Initial response: {resp_json}")
            resp_json = resp_json.get('data', {})

            # 检查是否有task_id
            if 'task_id' not in resp_json:
                print(f"  No task_id in response: {resp_json}")
                return

            task_id = resp_json['task_id']
            print(f"  Got task_id: {task_id}")

            # 轮询检查任务状态
            result = await check_task_status(session, api_key, task_id)

            # 计算耗时
            elapsed_time = time.time() - start_time

            # 从结果中提取页数和 token 数量
            if result:
                if 'json' in result and isinstance(result['json'], dict):
                    # 尝试从 json 结果中提取元数据
                    json_data = result['json']
                    if 'metadata' in json_data:
                        page_count = json_data['metadata'].get('page_count', 0)
                        token_count = json_data['metadata'].get('token_count', 0)

            if result and SAVE_FILE:
                base_name = file_path.stem
                md_path = output_dir / f"{base_name}.md"
                json_path = output_dir / f"{base_name}.json"

                if 'markdown' in result:
                    md_path.write_text(result['markdown'], encoding='utf-8')
                    print(f"  Markdown 已保存: {md_path}")
                if 'json' in result:
                    json_path.write_text(json.dumps(
                        result['json'], ensure_ascii=False, indent=2), encoding='utf-8')
                    print(f"  JSON 已保存: {json_path}")

                print(f"\n=== 解析完成统计 ===")
                print(f"  页数: {page_count}")
                print(f"  耗时: {elapsed_time:.2f} 秒")
                print(f"===================\n")
            elif result:
                print(f"  完成: {file_path.name}")
            else:
                print(f"  失败或超时: {file_path.name}")

    except Exception as e:
        print(f"  请求失败: {e}")


async def main():
    async with aiohttp.ClientSession() as session:
        for file_path in files_list:
            await process_file_async(session, file_path)


if __name__ == "__main__":
    asyncio.run(main())
