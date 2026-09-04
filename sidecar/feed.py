"""Generate an RSS feed from dated gemtext posts."""

from __future__ import annotations

import errno
import hashlib
import html
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM_NS)

FEED_FILENAME = "feed.rss"
GENERATED_FEED_FILENAME = ".feed.rss.generated-by-bottled-gemini"
MAX_POST_BYTES = 1 * 1024 * 1024
_POST_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<slug>.+))?\.gmi$")
_H1_RE = re.compile(r"^#(?!#)[ \t]*(?P<title>\S.*)$")
_OWNERSHIP_PLACEHOLDER = b"0" * 64
_OWNERSHIP_RE = re.compile(rb"<!-- bottled-gemini-sha256:([0-9a-f]{64}) -->")

FeedSignature = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Post:
    relative_path: str
    published: date
    title: str
    summary: str


def _post_candidates(content_dir: Path) -> list[tuple[Path, date]]:
    posts_dir = content_dir / "posts"
    if posts_dir.is_symlink() or not posts_dir.is_dir():
        return []

    candidates: list[tuple[Path, date]] = []
    for path in posts_dir.rglob("*.gmi"):
        relative_to_posts = path.relative_to(posts_dir)
        if any(part.startswith(".") for part in relative_to_posts.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        match = _POST_FILENAME_RE.fullmatch(path.name)
        if match is None:
            continue
        try:
            published = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        candidates.append((path, published))
    return candidates


def _fallback_title(path: Path) -> str:
    match = _POST_FILENAME_RE.fullmatch(path.name)
    slug = match.group("slug") if match is not None else path.stem
    return (slug or "Untitled").replace("-", " ").replace("_", " ").strip().title()


def _open_contained(path: Path, content_dir: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        Path(f"/proc/self/fd/{fd}").resolve(strict=True).relative_to(content_dir)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {path}")
    except (OSError, ValueError):
        os.close(fd)
        raise
    return fd


def _post_text(path: Path, text: str) -> tuple[str, str]:
    lines = text.splitlines()
    title: str | None = None
    in_preformatted = False
    for line in lines:
        if line.startswith("```"):
            in_preformatted = not in_preformatted
            continue
        if in_preformatted:
            continue
        heading = _H1_RE.fullmatch(line)
        if heading is not None:
            title = heading.group("title").strip()
            break

    paragraph: list[str] = []
    in_preformatted = False
    for line in lines:
        stripped = line.strip()
        if line.startswith("```"):
            in_preformatted = not in_preformatted
            continue
        if in_preformatted:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if line.startswith(("#", "=>", "* ", ">")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)

    resolved_title = title or _fallback_title(path)
    return resolved_title, " ".join(paragraph) or resolved_title


def _load_posts(content_dir: Path, candidates: list[tuple[Path, date]]) -> tuple[list[Post], FeedSignature]:
    posts: list[Post] = []
    signature: list[tuple[str, str]] = []
    for path, published in candidates:
        try:
            fd = _open_contained(path, content_dir)
        except ValueError:
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                continue
            raise
        with os.fdopen(fd, "rb") as stream:
            body = stream.read(MAX_POST_BYTES + 1)
        if len(body) > MAX_POST_BYTES:
            raise OSError(errno.EFBIG, f"post exceeds {MAX_POST_BYTES} bytes: {path}")
        title, summary = _post_text(path, body.decode("utf-8", errors="replace"))
        relative_path = path.relative_to(content_dir).as_posix()
        posts.append(
            Post(
                relative_path=relative_path,
                published=published,
                title=title,
                summary=summary,
            )
        )
        signature.append((relative_path, hashlib.sha256(body).hexdigest()))
    posts.sort(key=lambda post: (post.published, post.relative_path), reverse=True)
    return posts, tuple(sorted(signature))


def _xml_safe(value: str) -> str:
    def valid(character: str) -> bool:
        codepoint = ord(character)
        return (
            codepoint in (0x09, 0x0A, 0x0D)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )

    return "".join(character if valid(character) else "\uFFFD" for character in value)


def _rss_document(hostname: str, posts: list[Post]) -> bytes:
    site_url = f"gemini://{hostname}/"
    feed_url = f"{site_url}{FEED_FILENAME}"

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{hostname} gemlog"
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = f"Gemlog posts from {hostname}"
    ET.SubElement(channel, "generator").text = "bottled-gemini"
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    for post in posts:
        post_url = f"{site_url}{quote(post.relative_path, safe='/')}"
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = _xml_safe(post.title)
        ET.SubElement(item, "link").text = post_url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = post_url
        published = datetime.combine(post.published, time.min, tzinfo=timezone.utc)
        ET.SubElement(item, "pubDate").text = format_datetime(published, usegmt=True)
        ET.SubElement(item, "description").text = html.escape(_xml_safe(post.summary))

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    marker = b"<!-- bottled-gemini-sha256:" + _OWNERSHIP_PLACEHOLDER + b" -->"
    body = body.replace(b"?>", b"?>\n" + marker, 1)
    digest = hashlib.sha256(body).hexdigest().encode()
    return body.replace(_OWNERSHIP_PLACEHOLDER, digest, 1)


def _atomic_write(path: Path, body: bytes, mode: int = 0o644) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _managed_feed_link(feed_path: Path) -> bool | None:
    """Return True for our link, False for user content, and None when absent."""
    if feed_path.is_symlink():
        return os.readlink(feed_path) == GENERATED_FEED_FILENAME
    if feed_path.exists():
        return False
    return None


def _generated_feed_is_intact(path: Path, content_dir: Path) -> bool:
    fd = _open_contained(path, content_dir)
    with os.fdopen(fd, "rb") as stream:
        body = stream.read()
    match = _OWNERSHIP_RE.search(body)
    if match is None:
        return False
    expected = match.group(1)
    unstamped = body[: match.start(1)] + _OWNERSHIP_PLACEHOLDER + body[match.end(1) :]
    return hashlib.sha256(unstamped).hexdigest().encode() == expected


def _create_managed_feed_link(feed_path: Path) -> bool:
    try:
        os.symlink(GENERATED_FEED_FILENAME, feed_path)
    except FileExistsError:
        return _managed_feed_link(feed_path) is True
    return True


def refresh_feed(content_dir: Path, hostname: str, previous: FeedSignature | None) -> FeedSignature:
    """Refresh ``feed.rss`` when dated posts have changed and return their signature."""
    content_dir.mkdir(parents=True, exist_ok=True)
    content_dir = content_dir.resolve()
    feed_path = content_dir / FEED_FILENAME
    generated_path = content_dir / GENERATED_FEED_FILENAME
    managed = _managed_feed_link(feed_path)
    if managed is False:
        return previous or ()

    posts, signature = _load_posts(content_dir, _post_candidates(content_dir))
    should_write = (
        signature != previous
        or managed is None
        or generated_path.is_symlink()
        or not generated_path.is_file()
    )
    if managed is True and should_write and generated_path.exists():
        if generated_path.is_symlink() or not _generated_feed_is_intact(generated_path, content_dir):
            return signature
    if should_write:
        _atomic_write(generated_path, _rss_document(hostname, posts))
    if managed is None and not _create_managed_feed_link(feed_path):
        return previous or ()
    return signature
