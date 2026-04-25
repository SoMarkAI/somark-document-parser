import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATHS = [
    REPO_ROOT / "skills" / "contract-reviewer" / "contract_reviewer.py",
    REPO_ROOT / "skills" / "document-diff" / "document_diff.py",
    REPO_ROOT / "skills" / "financial-report-analyzer" / "financial_report_analyzer.py",
    REPO_ROOT / "skills" / "paper-digest" / "paper_digest.py",
    REPO_ROOT / "skills" / "pitch-screener" / "pitch_screener.py",
    REPO_ROOT / "skills" / "resume-parser" / "resume_parser.py",
    REPO_ROOT / "skills" / "somark-document-parser" / "somark_parser.py",
    REPO_ROOT / "skills" / "tender-analyzer" / "tender_analyzer.py",
]


def install_aiohttp_stub() -> None:
    sys.modules.setdefault(
        "aiohttp",
        types.SimpleNamespace(
            ClientSession=object,
            FormData=object,
            TCPConnector=object,
        ),
    )


def load_module(script_path: Path) -> Any:
    module_name = "test_" + "_".join(script_path.parts[-3:]).replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    status = 200

    def __init__(self, events: list[tuple[str, str, float]], name: str) -> None:
        self.events = events
        self.name = name

    async def __aenter__(self) -> "FakeResponse":
        self.events.append((self.name, "start", time.monotonic()))
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.events.append((self.name, "end", time.monotonic()))

    async def json(self) -> dict[str, bool]:
        return {"ok": True}


class FakeSession:
    def __init__(self, events: list[tuple[str, str, float]]) -> None:
        self.events = events

    def post(self, _url: str, **kwargs: Any) -> FakeResponse:
        name = str(kwargs["data"]["name"])
        return FakeResponse(self.events, name)


def assert_no_overlap(events: list[tuple[str, str, float]]) -> None:
    active: set[str] = set()
    max_active = 0

    for name, action, _timestamp in events:
        if action == "start":
            active.add(name)
            max_active = max(max_active, len(active))
        elif action == "end":
            active.remove(name)

    if max_active != 1:
        raise AssertionError(f"检测到同进程并发请求，max_active={max_active}")


async def call_post_json(module: Any, session: FakeSession, name: str) -> None:
    status, body = await module.post_json(
        session,
        "https://example.invalid/parse",
        data={"name": name},
    )
    if status != 200 or body != {"ok": True}:
        raise AssertionError(f"{name} 返回异常: status={status}, body={body}")


async def verify_script(script_path: Path) -> None:
    module = load_module(script_path)
    if not hasattr(module, "post_json"):
        raise AssertionError(f"脚本缺少 post_json: {script_path}")

    events: list[tuple[str, str, float]] = []
    session = FakeSession(events)
    await asyncio.gather(
        call_post_json(module, session, "A"),
        call_post_json(module, session, "B"),
    )
    assert_no_overlap(events)

    relative_path = script_path.relative_to(REPO_ROOT)
    order = " -> ".join(f"{name} {action}" for name, action, _ in events)
    print(f"{relative_path}: {order}")


async def main() -> None:
    install_aiohttp_stub()
    for script_path in SCRIPT_PATHS:
        await verify_script(script_path)

    print("验证通过：所有异步 skill 脚本同一进程内并发请求限制为 1")


if __name__ == "__main__":
    asyncio.run(main())
