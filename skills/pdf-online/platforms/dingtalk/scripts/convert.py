#!/usr/bin/env python
"""Stable NDJSON CLI for SoMark-to-DingTalk publish and resume."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from somark_dingtalk.publish import publish, resume  # noqa: E402


def _default_evidence(source: str | None, route: str) -> Path:
    stem = (Path(source).stem if source else "source") or "source"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / "output" / "somark-to-dingtalk" / f"{stem}-{route}-{timestamp}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="convert.py")
    commands = parser.add_subparsers(dest="command", required=True)

    publish_parser = commands.add_parser(
        "publish", help="plan and run one DingTalk route from explicit SoMark artifacts"
    )
    publish_parser.add_argument("--source")
    publish_parser.add_argument("--route", required=True, choices=("document", "sheet", "aitable"))
    publish_parser.add_argument("--title", required=True)
    publish_parser.add_argument("--profile")
    publish_parser.add_argument("--evidence-dir")
    publish_parser.add_argument("--mode", choices=("fast", "strict"), default="fast")
    publish_parser.add_argument("--markdown", dest="markdown_path")
    publish_parser.add_argument("--json", dest="json_path")
    publish_parser.add_argument("--assets", dest="assets_dir")
    publish_parser.add_argument("--timezone", default="Asia/Shanghai")
    publish_parser.add_argument(
        "--table-index",
        type=int,
        help="1-based SoMark table selection for AI Table when JSON contains multiple tables",
    )
    publish_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="run local planning from explicit artifacts without creating a DingTalk target",
    )
    publish_parser.add_argument(
        "--preview-first",
        action="store_true",
        help=(
            "for the sheet route, return after verified base-cell writes so the "
            "preview URL can be delivered before resume applies layout enhancements"
        ),
    )

    resume_parser = commands.add_parser(
        "resume",
        help="replay events and continue deferred work on the existing DingTalk target",
    )
    resume_parser.add_argument("--manifest", required=True)
    resume_parser.add_argument(
        "--profile",
        help="the same explicit DWS profile used by the original partial publish",
    )
    return parser


def _emit_failure(message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"event": "failed", "stage": "failed", "error": {"message": message}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resume":
            result = resume(args.manifest, profile=args.profile)
        else:
            evidence = (
                Path(args.evidence_dir).expanduser()
                if args.evidence_dir
                else _default_evidence(
                    args.source or args.json_path or args.markdown_path, args.route
                )
            )
            result = publish(
                source=args.source,
                route=args.route,
                title=args.title,
                evidence_dir=evidence,
                profile=args.profile,
                mode=args.mode,
                execute=not args.plan_only,
                markdown_path=args.markdown_path,
                json_path=args.json_path,
                assets_dir=args.assets_dir,
                timezone=args.timezone,
                table_index=args.table_index,
                preview_first=args.preview_first,
            )
    except Exception as exc:
        _emit_failure(str(exc))
        return 1
    if result.stage == "failed":
        return 1
    if result.stage == "partial":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
