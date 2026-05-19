from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from blackboard.kernel.atomic_files import write_bytes_atomically

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".ico"}
_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE | re.DOTALL)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((\S+?)(?:\s+\"([^\"]*)\")?\)", re.IGNORECASE)
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_FILE_URI_PREFIX = "file://"
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_MIME_EXTENSION_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}


@dataclass
class _ContentImageCandidate:
    start: int
    end: int
    source: str
    alt: str = ""
    title: str = ""
    kind: str = "markdown"


@dataclass
class _ResolvedAttachment:
    src: str
    alt: str = ""
    title: str = ""
    content_type: str = ""
    size: int = 0
    filename: str = ""
    persisted: bool = False


def _attachment_root(data_root: Path, project_id: str, session_id: str, message_id: str) -> Path:
    return Path(data_root) / "projects" / str(project_id or "") / "chat_attachments" / str(session_id or "") / str(message_id or "")


def chat_attachment_url(project_id: str, session_id: str, message_id: str, filename: str) -> str:
    return (
        f"/api/chat/{quote(str(project_id or ''), safe='')}/sessions/"
        f"{quote(str(session_id or ''), safe='')}/attachments/"
        f"{quote(str(message_id or ''), safe='')}/{quote(str(filename or ''), safe='')}"
    )


def resolve_chat_attachment_path(data_root: Path, project_id: str, session_id: str, message_id: str, filename: str) -> Path:
    if not _SAFE_FILENAME_RE.match(str(filename or "")):
        raise ValueError("invalid attachment filename")
    root = _attachment_root(Path(data_root), project_id, session_id, message_id).resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("invalid attachment path") from exc
    return path


def persist_chat_images(
    *,
    data_root: Path,
    project_id: str,
    session_id: str,
    message_id: str,
    project_root: str = "",
    content: str = "",
    raw: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    value = str(content or "")
    roots = _allowed_roots(data_root=Path(data_root), project_root=project_root)
    replacements: List[Tuple[int, int, str]] = []
    images: List[Dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_urls: set[str] = set()

    for candidate in _content_candidates(value):
        resolved = _resolve_source(
            candidate.source,
            alt=candidate.alt,
            title=candidate.title,
            data_root=Path(data_root),
            project_id=project_id,
            session_id=session_id,
            message_id=message_id,
            roots=roots,
        )
        if resolved is None:
            continue
        replacement = _replacement_markup(candidate.kind, resolved)
        replacements.append((candidate.start, candidate.end, replacement))
        if resolved.src not in seen_urls:
            seen_urls.add(resolved.src)
            images.append(_attachment_dict(resolved))
        seen_sources.add(candidate.source)

    rewritten = _apply_replacements(value, replacements)

    for raw_candidate in _raw_image_candidates(raw or {}):
        source = str(raw_candidate.get("source") or "").strip()
        if not source or source in seen_sources:
            continue
        resolved = _resolve_source(
            source,
            alt=str(raw_candidate.get("alt") or "").strip(),
            title=str(raw_candidate.get("title") or "").strip(),
            data_root=Path(data_root),
            project_id=project_id,
            session_id=session_id,
            message_id=message_id,
            roots=roots,
        )
        if resolved is None or resolved.src in seen_urls:
            continue
        seen_urls.add(resolved.src)
        images.append(_attachment_dict(resolved))

    return rewritten, images


def register_chat_images(
    *,
    data_root: Path,
    project_id: str,
    session_id: str,
    message_id: str,
    project_root: str = "",
    attachments: Iterable[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    roots = _allowed_roots(data_root=Path(data_root), project_root=project_root)
    images: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in list(attachments or [])[:16]:
        resolved = _resolve_registered_attachment(
            item,
            data_root=Path(data_root),
            project_id=project_id,
            session_id=session_id,
            message_id=message_id,
            roots=roots,
        )
        if resolved is None or resolved.src in seen_urls:
            continue
        seen_urls.add(resolved.src)
        images.append(_attachment_dict(resolved))
    return images


def _attachment_dict(value: _ResolvedAttachment) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"src": value.src}
    if value.alt:
        payload["alt"] = value.alt
    if value.title:
        payload["title"] = value.title
    if value.content_type:
        payload["content_type"] = value.content_type
    if value.size:
        payload["size"] = int(value.size)
    if value.filename:
        payload["filename"] = value.filename
    if value.persisted:
        payload["persisted"] = True
    return payload


def _resolve_registered_attachment(
    item: Mapping[str, Any],
    *,
    data_root: Path,
    project_id: str,
    session_id: str,
    message_id: str,
    roots: List[Path],
) -> Optional[_ResolvedAttachment]:
    alt = str(item.get("alt") or item.get("label") or "").strip()
    title = str(item.get("title") or item.get("name") or "").strip()
    filename_hint = str(item.get("filename") or item.get("name") or "").strip()
    base64_payload = str(item.get("content_base64") or item.get("base64") or "").strip()
    if base64_payload:
        try:
            payload = base64.b64decode(re.sub(r"\s+", "", base64_payload), validate=True)
        except (binascii.Error, ValueError):
            return None
        if not payload:
            return None
        content_type = str(item.get("content_type") or item.get("mime") or "").strip().lower()
        if not content_type and filename_hint:
            content_type = str(mimetypes.guess_type(filename_hint)[0] or "").lower()
        if not content_type.startswith("image/"):
            return None
        suffix = _normalized_extension(Path(filename_hint).suffix) if filename_hint else ""
        filename = _persist_bytes(
            payload,
            mime=content_type,
            data_root=data_root,
            project_id=project_id,
            session_id=session_id,
            message_id=message_id,
            suffix=suffix,
        )
        return _ResolvedAttachment(
            src=chat_attachment_url(project_id, session_id, message_id, filename),
            alt=alt or Path(filename_hint).stem or "Assistant image",
            title=title or filename_hint,
            content_type=content_type,
            size=len(payload),
            filename=filename,
            persisted=True,
        )
    source = str(item.get("source") or item.get("src") or item.get("path") or item.get("data_url") or "").strip()
    if not source:
        return None
    if source.startswith("http://") or source.startswith("https://"):
        return None
    return _resolve_source(
        source,
        alt=alt,
        title=title,
        data_root=data_root,
        project_id=project_id,
        session_id=session_id,
        message_id=message_id,
        roots=roots,
    )


def _allowed_roots(*, data_root: Path, project_root: str) -> List[Path]:
    roots = [Path(data_root).resolve()]
    project_root_text = str(project_root or "").strip()
    if project_root_text:
        try:
            roots.append(Path(project_root_text).resolve())
        except Exception:
            pass
    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _content_candidates(text: str) -> List[_ContentImageCandidate]:
    out: List[_ContentImageCandidate] = []
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        out.append(_ContentImageCandidate(
            start=match.start(),
            end=match.end(),
            source=str(match.group(2) or "").strip(),
            alt=str(match.group(1) or "").strip(),
            title=str(match.group(3) or "").strip(),
            kind="markdown",
        ))
    for match in _HTML_IMAGE_RE.finditer(text):
        tag = match.group(0) or ""
        src = _html_attr(tag, "src")
        if not src:
            continue
        out.append(_ContentImageCandidate(
            start=match.start(),
            end=match.end(),
            source=src,
            alt=_html_attr(tag, "alt"),
            title=_html_attr(tag, "title"),
            kind="html",
        ))
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and _looks_like_local_image_source(stripped):
            line_start = offset + line.index(stripped)
            out.append(_ContentImageCandidate(
                start=line_start,
                end=line_start + len(stripped),
                source=stripped,
                alt="Assistant image",
                title="",
                kind="line",
            ))
        offset += len(line)
    out.sort(key=lambda item: (item.start, item.end))
    deduped: List[_ContentImageCandidate] = []
    for item in out:
        if deduped and item.start < deduped[-1].end:
            continue
        deduped.append(item)
    return deduped


def _apply_replacements(text: str, replacements: Iterable[Tuple[int, int, str]]) -> str:
    ordered = sorted(replacements, key=lambda item: (item[0], item[1]))
    if not ordered:
        return text
    parts: List[str] = []
    last = 0
    for start, end, replacement in ordered:
        if start < last:
            continue
        parts.append(text[last:start])
        parts.append(replacement)
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _replacement_markup(kind: str, attachment: _ResolvedAttachment) -> str:
    title_part = f' "{attachment.title}"' if attachment.title else ""
    alt = attachment.alt or "Assistant image"
    if kind == "html":
        title_attr = f' title="{_escape_html_attr(attachment.title)}"' if attachment.title else ""
        alt_attr = _escape_html_attr(alt)
        return f'<img src="{_escape_html_attr(attachment.src)}" alt="{alt_attr}"{title_attr}>'
    return f'![{alt}]({attachment.src}{title_part})'


def _escape_html_attr(value: str) -> str:
    return str(value or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _html_attr(tag: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))", str(tag or ""), re.IGNORECASE)
    return str(match.group(1) or match.group(2) or match.group(3) or "").strip() if match else ""


def _raw_image_candidates(raw: Any, *, depth: int = 0) -> List[Dict[str, str]]:
    if depth > 4:
        return []
    out: List[Dict[str, str]] = []
    if isinstance(raw, dict):
        direct = str(raw.get("src") or raw.get("url") or raw.get("image_url") or raw.get("path") or raw.get("file") or "").strip()
        if direct and _looks_like_image_source(direct):
            out.append({
                "source": direct,
                "alt": str(raw.get("alt") or raw.get("label") or raw.get("name") or "").strip(),
                "title": str(raw.get("title") or raw.get("name") or "").strip(),
            })
        for value in raw.values():
            out.extend(_raw_image_candidates(value, depth=depth + 1))
        return out[:16]
    if isinstance(raw, list):
        for item in raw[:16]:
            out.extend(_raw_image_candidates(item, depth=depth + 1))
        return out[:16]
    if isinstance(raw, str):
        text = raw.strip()
        if _looks_like_image_source(text):
            return [{"source": text, "alt": "", "title": ""}]
    return []


def _resolve_source(
    source: str,
    *,
    alt: str,
    title: str,
    data_root: Path,
    project_id: str,
    session_id: str,
    message_id: str,
    roots: List[Path],
) -> Optional[_ResolvedAttachment]:
    value = str(source or "").strip()
    if not value:
        return None
    if value.startswith("/api/chat/"):
        return _ResolvedAttachment(src=value, alt=alt, title=title)
    if value.startswith("http://") or value.startswith("https://"):
        return _ResolvedAttachment(src=value, alt=alt, title=title)
    data_match = _DATA_URL_RE.match(value)
    if data_match:
        mime = str(data_match.group(1) or "").lower()
        try:
            payload = base64.b64decode(re.sub(r"\s+", "", str(data_match.group(2) or "")), validate=True)
        except (binascii.Error, ValueError):
            return None
        if not payload:
            return None
        filename = _persist_bytes(payload, mime=mime, data_root=data_root, project_id=project_id, session_id=session_id, message_id=message_id)
        return _ResolvedAttachment(
            src=chat_attachment_url(project_id, session_id, message_id, filename),
            alt=alt,
            title=title,
            content_type=mime,
            size=len(payload),
            filename=filename,
            persisted=True,
        )
    local_path = _resolve_local_source(value, roots=roots)
    if local_path is None or not local_path.exists() or not local_path.is_file():
        return None
    mime = _guess_image_mime(local_path)
    if not mime.startswith("image/"):
        return None
    payload = local_path.read_bytes()
    if not payload:
        return None
    filename = _persist_bytes(payload, mime=mime, data_root=data_root, project_id=project_id, session_id=session_id, message_id=message_id, suffix=local_path.suffix)
    return _ResolvedAttachment(
        src=chat_attachment_url(project_id, session_id, message_id, filename),
        alt=alt or local_path.stem,
        title=title or local_path.name,
        content_type=mime,
        size=len(payload),
        filename=filename,
        persisted=True,
    )


def _persist_bytes(
    payload: bytes,
    *,
    mime: str,
    data_root: Path,
    project_id: str,
    session_id: str,
    message_id: str,
    suffix: str = "",
) -> str:
    digest = hashlib.sha256(payload).hexdigest()[:20]
    extension = _normalized_extension(suffix or _MIME_EXTENSION_MAP.get(str(mime or "").lower(), ""))
    filename = f"{digest}{extension or '.bin'}"
    path = resolve_chat_attachment_path(data_root, project_id, session_id, message_id, filename)
    if not path.exists():
        write_bytes_atomically(path, payload)
    return filename


def _normalized_extension(value: str) -> str:
    suffix = str(value or "").strip().lower()
    if not suffix:
        return ""
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix if suffix in _IMAGE_EXTENSIONS else ""


def _guess_image_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return str(mime or "application/octet-stream").lower()


def _resolve_local_source(source: str, *, roots: List[Path]) -> Optional[Path]:
    parsed = urlparse(source)
    if parsed.scheme and parsed.scheme.lower() == "file":
        candidate = Path(unquote(parsed.path.lstrip("/")))
    elif parsed.scheme and len(parsed.scheme) == 1 and re.match(r"^[A-Za-z]$", parsed.scheme):
        candidate = Path(source)
    elif parsed.scheme:
        return None
    else:
        candidate = Path(source)
    candidates: List[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        for root in roots:
            candidates.append(root / candidate)
    for possible in candidates:
        try:
            resolved = possible.expanduser().resolve()
        except Exception:
            continue
        if not _normalized_extension(resolved.suffix):
            continue
        for root in roots:
            try:
                resolved.relative_to(root)
                if resolved.exists() and resolved.is_file():
                    return resolved
            except ValueError:
                continue
    return None


def _looks_like_image_source(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("data:image/"):
        return True
    if text.startswith("http://") or text.startswith("https://"):
        return any(urlparse(text).path.lower().endswith(ext) for ext in _IMAGE_EXTENSIONS)
    if text.startswith("/api/chat/"):
        return True
    return _looks_like_local_image_source(text)


def _looks_like_local_image_source(value: str) -> bool:
    text = str(value or "").strip()
    if not text or "\n" in text:
        return False
    lower = text.lower()
    if lower.startswith(_FILE_URI_PREFIX):
        path = urlparse(text).path.lower()
        return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)
    if re.match(r"^[A-Za-z]:[\\/].+", text):
        return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)
    if text.startswith("./") or text.startswith("../") or text.startswith("/") or "\\" in text or "/" in text:
        return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)
