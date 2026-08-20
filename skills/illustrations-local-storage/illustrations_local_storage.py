#!/usr/bin/env python3
"""Download remote images in SoMark Markdown and rewrite them as local assets."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:  # Report a user-facing installation command at execution time.
    Image = None
    ImageOps = None
    UnidentifiedImageError = OSError

VERSION = "1.4.0"
DEFAULT_MAX_IMAGE_BYTES = 100 * 1024 * 1024
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
AMBIGUOUS_IMAGE_CONTENT_TYPES = {
    "application/octet-stream",
    "application/binary",
    "binary/octet-stream",
}

# SoMark currently emits normal Markdown images. HTML images are supported too,
# because tables and downstream transformations sometimes use them.
MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*(?:<(?P<angle_url>https?://[^>\r\n]+)>|"
    r"(?P<plain_url>https?://[^\s)\r\n]+))",
    re.IGNORECASE,
)
HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<quote>[\"'])(?P<url>https?://.*?)"
    r"(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})")
INVALID_FILENAME_CHARS_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
PACKAGE_NAME_CHARS_RE = re.compile(r"[ <>:\"/\\|?*]")
WINDOWS_RESERVED_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
KNOWN_IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".ico",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
CONTENT_TYPE_EXTENSIONS = {
    "image/apng": ".apng",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/heic-sequence": ".heic",
    "image/heif-sequence": ".heif",
    "image/vnd.microsoft.icon": ".ico",
    "image/x-icon": ".ico",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}
SVG_ROOT_RE = re.compile(
    br"^\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<svg(?:\s|>)",
    re.IGNORECASE | re.DOTALL,
)


class LocalizationError(RuntimeError):
    """A user-facing localization failure."""


@dataclass(frozen=True)
class UrlSpan:
    start: int
    end: int
    url: str


@dataclass(frozen=True)
class DownloadResult:
    url: str
    temp_path: Path
    content_type: str
    size: int


@dataclass(frozen=True)
class LocalizationResult:
    markdown_path: Path
    image_dir: Path
    references: int
    unique_images: int
    downloaded_images: int
    reused_images: int


@dataclass(frozen=True)
class BatchSuccess:
    input_path: Path
    result: LocalizationResult


@dataclass(frozen=True)
class BatchFailure:
    input_path: Path
    error: str


@dataclass(frozen=True)
class BatchLocalizationResult:
    output_dir: Path
    successes: list[BatchSuccess]
    failures: list[BatchFailure]


def _write_utf8(path: Path, content: str) -> None:
    """Write UTF-8 text with stable newlines on Python versions before 3.10."""
    with open(path, "w", encoding="utf-8", newline="") as output:
        output.write(content)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "下载 SoMark Markdown 中的远程图片，并把图片 URL 改为本地相对路径。"
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="官网导出的 Markdown 文件，或包含 Markdown 的目录",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="输出目录；单文件默认使用原 Markdown 文件名，目录输入沿用批量输出规则",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="并发下载数（默认：4）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="每次 HTTP 请求的超时秒数（默认：60）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="网络失败后的重试次数（默认：3，即最多请求 4 次）",
    )
    parser.add_argument(
        "--max-image-mb",
        type=float,
        default=DEFAULT_MAX_IMAGE_BYTES / 1024 / 1024,
        help="单张图片允许的最大体积，单位 MB（默认：100）",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help="只下载指定域名，可重复传入；默认不限制域名",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖内容不同的已有 Markdown 或图片文件",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def _protected_fence_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return fenced-code ranges so examples are not treated as real images."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    opening_start: int | None = None
    opening_char = ""
    opening_length = 0

    for line in markdown.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group("marker")
            if opening_start is None:
                opening_start = offset
                opening_char = marker[0]
                opening_length = len(marker)
            elif (
                marker[0] == opening_char
                and len(marker) >= opening_length
                and not line[match.end() :].strip()
            ):
                ranges.append((opening_start, offset + len(line)))
                opening_start = None
                opening_char = ""
                opening_length = 0
        offset += len(line)

    if opening_start is not None:
        ranges.append((opening_start, len(markdown)))
    return ranges


def _inside_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def find_remote_image_urls(markdown: str) -> list[UrlSpan]:
    """Find replaceable HTTP(S) image URL spans outside fenced code blocks."""
    protected = _protected_fence_ranges(markdown)
    spans: list[UrlSpan] = []

    for match in MARKDOWN_IMAGE_RE.finditer(markdown):
        if _inside_ranges(match.start(), protected):
            continue
        group = "angle_url" if match.group("angle_url") is not None else "plain_url"
        spans.append(
            UrlSpan(match.start(group), match.end(group), html.unescape(match.group(group)))
        )

    for match in HTML_IMAGE_RE.finditer(markdown):
        if _inside_ranges(match.start(), protected):
            continue
        spans.append(
            UrlSpan(match.start("url"), match.end("url"), html.unescape(match.group("url")))
        )

    spans.sort(key=lambda item: item.start)
    result: list[UrlSpan] = []
    previous_end = -1
    for span in spans:
        if span.start < previous_end:
            continue
        result.append(span)
        previous_end = span.end
    return result


def _normalize_allowed_hosts(hosts: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for host in hosts:
        value = host.strip().lower().rstrip(".")
        if not value or "://" in value or "/" in value:
            raise LocalizationError(f"无效的 --allowed-host：{host!r}，请只填写域名")
        normalized.add(value)
    return normalized


def _host_is_allowed(url: str, allowed_hosts: set[str]) -> bool:
    if not allowed_hosts:
        return True
    hostname = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return any(hostname == host or hostname.endswith("." + host) for host in allowed_hosts)


def _download_once(
    url: str,
    temp_path: Path,
    timeout: float,
    max_bytes: int,
    allowed_hosts: set[str],
) -> DownloadResult:
    if not _host_is_allowed(url, allowed_hosts):
        raise LocalizationError(f"图片域名不在允许列表中：{url}")

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": f"illustrations-local-storage/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not _host_is_allowed(final_url, allowed_hosts):
                raise LocalizationError(f"图片重定向到了未允许的域名：{final_url}")
            content_type = response.headers.get_content_type().lower()
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > max_bytes:
                    raise LocalizationError(
                        f"图片超过大小限制（{declared_size} bytes）：{url}"
                    )

            size = 0
            first_chunk = b""
            with temp_path.open("wb") as output:
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes - size + 1))
                    if not chunk:
                        break
                    if len(first_chunk) < 4096:
                        first_chunk += chunk[: 4096 - len(first_chunk)]
                    size += len(chunk)
                    if size > max_bytes:
                        raise LocalizationError(
                            f"图片超过大小限制（>{max_bytes} bytes）：{url}"
                        )
                    output.write(chunk)
    except LocalizationError:
        raise

    if size == 0:
        raise LocalizationError(f"下载到了空文件：{url}")
    detected_extension = _detect_image_extension(first_chunk)
    if content_type.startswith("image/"):
        if detected_extension is None:
            raise LocalizationError(
                f"图片 URL 的响应无法识别为有效图片（Content-Type: {content_type}）：{url}"
            )
    elif content_type in AMBIGUOUS_IMAGE_CONTENT_TYPES:
        if detected_extension is None:
            raise LocalizationError(
                f"图片 URL 返回的二进制内容不是可识别图片：{url}"
            )
    else:
        raise LocalizationError(
            f"图片 URL 返回了非图片内容（Content-Type: {content_type}）：{url}"
        )
    return DownloadResult(url, temp_path, content_type, size)


def _download_one(
    url: str,
    temp_path: Path,
    timeout: float,
    max_bytes: int,
    allowed_hosts: set[str],
    retries: int,
) -> DownloadResult:
    """Download an image, retrying transient network and server failures."""
    for attempt in range(retries + 1):
        try:
            return _download_once(url, temp_path, timeout, max_bytes, allowed_hosts)
        except LocalizationError:
            temp_path.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_HTTP_STATUS
            last_error: Exception = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            retryable = True
            last_error = exc

        temp_path.unlink(missing_ok=True)
        if not retryable or attempt >= retries:
            attempts = attempt + 1
            raise LocalizationError(
                f"下载图片失败（已请求 {attempts} 次）：{url}（{last_error}）"
            ) from last_error

        # 1s, 2s, 4s...; cap the delay so a high custom retry count stays sane.
        time.sleep(min(2**attempt, 8))

    raise AssertionError("unreachable")


def _detect_image_extension(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"BM"):
        return ".bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"avif", b"avis"}:
            return ".avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx"}:
            return ".heic"
        if brand in {b"heif", b"heim", b"heis", b"mif1", b"msf1"}:
            return ".heif"
    if SVG_ROOT_RE.match(header.lstrip(b"\xef\xbb\xbf")):
        return ".svg"
    return None


def _extensions_are_compatible(expected: str, detected: str) -> bool:
    groups = (
        {".jpg", ".jpeg", ".jfif"},
        {".tif", ".tiff"},
        {".png", ".apng"},
        {".heic", ".heif"},
    )
    return expected == detected or any(
        expected in group and detected in group for group in groups
    )


def _guess_extension(result: DownloadResult) -> str:
    with result.temp_path.open("rb") as downloaded:
        detected = _detect_image_extension(downloaded.read(4096))
    if detected:
        return detected
    return CONTENT_TYPE_EXTENSIONS.get(result.content_type, ".img")


def _safe_basename(url: str, index: int, result: DownloadResult) -> str:
    raw_name = urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(url).path).name)
    name = INVALID_FILENAME_CHARS_RE.sub("_", raw_name).strip(" .")
    if name in {"", ".", ".."}:
        name = f"image_{index + 1}"
    if len(name) > 180:
        suffix = Path(name).suffix
        stem_limit = max(1, 180 - len(suffix))
        name = Path(name).stem[:stem_limit] + suffix
    suffix = Path(name).suffix.lower()
    detected_extension = _guess_extension(result)
    if suffix not in KNOWN_IMAGE_EXTENSIONS:
        name += detected_extension
    elif not _extensions_are_compatible(suffix, detected_extension):
        name = Path(name).stem + detected_extension
    if Path(name).stem.casefold() in WINDOWS_RESERVED_STEMS:
        name = "_" + name
    return name


def _assign_filenames(
    urls: list[str], results: dict[str, DownloadResult]
) -> dict[str, str]:
    provisional: dict[str, str] = {}
    filename_owner: dict[str, str] = {}
    for index, url in enumerate(urls):
        original = _safe_basename(url, index, results[url])
        candidate = original
        collision_key = candidate.casefold()
        if collision_key in filename_owner and filename_owner[collision_key] != url:
            path = Path(original)
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            candidate = f"{path.stem}_{digest}{path.suffix}"
            counter = 2
            while candidate.casefold() in filename_owner:
                candidate = f"{path.stem}_{digest}_{counter}{path.suffix}"
                counter += 1
            collision_key = candidate.casefold()
        filename_owner[collision_key] = url
        provisional[url] = candidate

    # Match SoMark ZIP naming: sort by the algorithmic on-disk name first,
    # then replace category-specific names with one global numeric sequence.
    sorted_urls = sorted(urls, key=lambda url: (provisional[url], url))
    return {
        url: f"image_{position:03d}.jpg"
        for position, url in enumerate(sorted_urls, start=1)
    }


def _convert_to_jpeg(source: Path, destination: Path, url: str) -> None:
    if Image is None or ImageOps is None:
        raise LocalizationError(
            '缺少 Pillow；请先执行：python -m pip install "Pillow>=9.4.0,<11.0.0"'
        )

    try:
        with Image.open(source) as opened:
            opened.seek(0)
            oriented = ImageOps.exif_transpose(opened)
            try:
                oriented.load()
                has_alpha = oriented.mode in {"RGBA", "LA"} or (
                    oriented.mode == "P" and "transparency" in oriented.info
                )
                if has_alpha:
                    rgba = oriented.convert("RGBA")
                    converted = Image.new("RGB", rgba.size, (255, 255, 255))
                    converted.paste(rgba, mask=rgba.getchannel("A"))
                    rgba.close()
                else:
                    converted = oriented.convert("RGB")
                try:
                    converted.save(
                        destination,
                        format="JPEG",
                        quality=95,
                        subsampling=0,
                        optimize=True,
                    )
                finally:
                    converted.close()
            finally:
                if oriented is not opened:
                    oriented.close()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        destination.unlink(missing_ok=True)
        raise LocalizationError(f"图片转换为 JPEG 失败：{url}（{exc}）") from exc


def _files_equal(left: Path, right: Path) -> bool:
    if not left.exists() or left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _validate_image_dir(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LocalizationError("--image-dir 必须是输出目录内的安全相对路径")
    return path


def _rewrite_markdown(
    markdown: str,
    spans: list[UrlSpan],
    filenames: dict[str, str],
    image_dir: PurePosixPath,
) -> str:
    chunks: list[str] = []
    cursor = 0
    for span in spans:
        chunks.append(markdown[cursor : span.start])
        relative_path = "./" + str(image_dir / filenames[span.url])
        chunks.append(urllib.parse.quote(relative_path, safe="/-._~"))
        cursor = span.end
    chunks.append(markdown[cursor:])
    return "".join(chunks)


def localize_markdown(
    input_path: Path,
    output_dir: Path,
    *,
    image_dir_name: str = "images",
    workers: int = 4,
    timeout: float = 60.0,
    retries: int = 3,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    allowed_hosts: Iterable[str] = (),
    force: bool = False,
) -> LocalizationResult:
    input_path = input_path.resolve()
    output_dir = output_dir.resolve()
    if not input_path.is_file():
        raise LocalizationError(f"输入文件不存在：{input_path}")
    if input_path.suffix.lower() not in {".md", ".markdown"}:
        raise LocalizationError("输入文件必须是 .md 或 .markdown")
    if workers < 1:
        raise LocalizationError("--workers 必须大于 0")
    if timeout <= 0:
        raise LocalizationError("--timeout 必须大于 0")
    if retries < 0:
        raise LocalizationError("--retries 不能小于 0")
    if max_image_bytes < 1:
        raise LocalizationError("--max-image-mb 必须大于 0")

    image_dir = _validate_image_dir(image_dir_name)
    normalized_hosts = _normalize_allowed_hosts(allowed_hosts)
    markdown = input_path.read_text(encoding="utf-8-sig")
    spans = find_remote_image_urls(markdown)
    urls = list(dict.fromkeys(span.url for span in spans))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_markdown = output_dir / "main.md"
    output_image_dir = output_dir.joinpath(*image_dir.parts)
    output_image_dir.mkdir(parents=True, exist_ok=True)

    if not urls:
        if output_markdown.exists() and output_markdown.read_text(encoding="utf-8") != markdown and not force:
            raise LocalizationError(
                f"输出 Markdown 已存在且内容不同：{output_markdown}；如需覆盖请加 --force"
            )
        _write_utf8(output_markdown, markdown)
        return LocalizationResult(output_markdown, output_image_dir, 0, 0, 0, 0)

    with tempfile.TemporaryDirectory(prefix=".somark-images-", dir=output_dir) as temp:
        temp_dir = Path(temp)
        results: dict[str, DownloadResult] = {}
        errors: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
            future_urls = {
                pool.submit(
                    _download_one,
                    url,
                    temp_dir / f"{index:06d}.download",
                    timeout,
                    max_image_bytes,
                    normalized_hosts,
                    retries,
                ): url
                for index, url in enumerate(urls)
            }
            for future in concurrent.futures.as_completed(future_urls):
                url = future_urls[future]
                try:
                    results[url] = future.result()
                except Exception as exc:  # collect all failures for one useful report
                    errors.append(str(exc))

        if errors:
            details = "\n  - ".join(errors)
            raise LocalizationError(f"有 {len(errors)} 张图片下载失败：\n  - {details}")

        filenames = _assign_filenames(urls, results)
        converted_results: dict[str, DownloadResult] = {}
        for index, url in enumerate(urls):
            converted_path = temp_dir / f"{index:06d}.jpg"
            _convert_to_jpeg(results[url].temp_path, converted_path, url)
            converted_results[url] = DownloadResult(
                url,
                converted_path,
                "image/jpeg",
                converted_path.stat().st_size,
            )
        results = converted_results
        rewritten = _rewrite_markdown(markdown, spans, filenames, image_dir)

        if output_markdown.exists():
            existing = output_markdown.read_text(encoding="utf-8")
            if existing != rewritten and not force:
                raise LocalizationError(
                    f"输出 Markdown 已存在且内容不同：{output_markdown}；如需覆盖请加 --force"
                )

        reused = 0
        for url in urls:
            destination = output_image_dir / filenames[url]
            staged = results[url].temp_path
            if destination.exists():
                if _files_equal(destination, staged):
                    reused += 1
                    continue
                if not force:
                    raise LocalizationError(
                        f"图片文件已存在且内容不同：{destination}；如需覆盖请加 --force"
                    )

        for url in urls:
            destination = output_image_dir / filenames[url]
            staged = results[url].temp_path
            if destination.exists() and _files_equal(destination, staged):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)

        temp_markdown = temp_dir / "main.md"
        _write_utf8(temp_markdown, rewritten)
        os.replace(temp_markdown, output_markdown)

    return LocalizationResult(
        output_markdown,
        output_image_dir,
        len(spans),
        len(urls),
        len(urls) - reused,
        reused,
    )


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def localize_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    image_dir_name: str = "images",
    workers: int = 4,
    timeout: float = 60.0,
    retries: int = 3,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    allowed_hosts: Iterable[str] = (),
    force: bool = False,
) -> BatchLocalizationResult:
    """Recursively localize every Markdown document into an isolated package."""
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if not input_dir.is_dir():
        raise LocalizationError(f"输入目录不存在：{input_dir}")
    if _path_is_within(output_dir, input_dir):
        raise LocalizationError("批量输出目录不能位于输入目录内部，避免重复处理输出文件")
    allowed_hosts = tuple(allowed_hosts)

    markdown_files = sorted(
        (
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".markdown"}
        ),
        key=lambda path: path.relative_to(input_dir).as_posix().casefold(),
    )
    if not markdown_files:
        raise LocalizationError(f"输入目录中没有 .md 或 .markdown 文件：{input_dir}")

    package_owners: dict[str, Path] = {}
    packages: list[tuple[Path, Path]] = []
    for input_path in markdown_files:
        relative = input_path.relative_to(input_dir)
        package_name = _sanitize_package_name(input_path.stem)
        package_dir = output_dir / relative.parent / package_name
        package_key = str(package_dir).casefold()
        previous = package_owners.get(package_key)
        if previous is not None:
            raise LocalizationError(
                "批量输入包含会映射到同一输出目录的文件："
                f"{previous} 和 {input_path}"
            )
        package_owners[package_key] = input_path
        packages.append((input_path, package_dir))

    successes: list[BatchSuccess] = []
    failures: list[BatchFailure] = []
    for input_path, package_dir in packages:
        try:
            result = localize_markdown(
                input_path,
                package_dir,
                image_dir_name=image_dir_name,
                workers=workers,
                timeout=timeout,
                retries=retries,
                max_image_bytes=max_image_bytes,
                allowed_hosts=allowed_hosts,
                force=force,
            )
        except (LocalizationError, UnicodeError, OSError) as exc:
            failures.append(BatchFailure(input_path, str(exc)))
        else:
            successes.append(BatchSuccess(input_path, result))

    return BatchLocalizationResult(output_dir, successes, failures)


def _default_output_dir(input_path: Path) -> Path:
    if not input_path.is_dir():
        return input_path.parent / _sanitize_package_name(input_path.stem)

    source_name = _sanitize_package_name(input_path.name)
    match = re.fullmatch(r"input(?P<suffix>_.*)?", source_name, re.IGNORECASE)
    if match:
        output_name = "output" + (match.group("suffix") or "")
    else:
        output_name = f"{source_name}_localized"
    return input_path.parent / output_name


def _sanitize_package_name(name: str) -> str:
    sanitized = PACKAGE_NAME_CHARS_RE.sub("_", name)
    return sanitized or "_"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir or _default_output_dir(args.input)
    common_options = {
        "workers": args.workers,
        "timeout": args.timeout,
        "retries": args.retries,
        "max_image_bytes": int(args.max_image_mb * 1024 * 1024),
        "allowed_hosts": args.allowed_host,
        "force": args.force,
    }

    if args.input.is_dir():
        try:
            batch = localize_directory(args.input, output_dir, **common_options)
        except (LocalizationError, UnicodeError, OSError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1

        for item in batch.successes:
            print(f"[完成] {item.input_path} -> {item.result.markdown_path.parent}")
        for item in batch.failures:
            print(f"[失败] {item.input_path}：{item.error}", file=sys.stderr)
        print(
            f"批量转换完成：成功 {len(batch.successes)}，"
            f"失败 {len(batch.failures)}，输出目录：{batch.output_dir}"
        )
        return 1 if batch.failures else 0

    try:
        result = localize_markdown(
            args.input,
            output_dir,
            **common_options,
        )
    except (LocalizationError, UnicodeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"完成：{result.markdown_path}")
    print(f"图片目录：{result.image_dir}")
    print(
        "图片引用："
        f"{result.references}，唯一图片：{result.unique_images}，"
        f"新下载：{result.downloaded_images}，复用：{result.reused_images}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
