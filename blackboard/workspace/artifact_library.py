from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from blackboard.kernel.atomic_files import write_text_atomically


def artifact_library_dir(data_root: Path) -> Path:
    target = Path(data_root).resolve() / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _artifact_dir(data_root: Path, artifact_id: str, *, create: bool = True) -> Path:
    target = artifact_library_dir(data_root) / str(artifact_id).strip()
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _manifest_path(data_root: Path, artifact_id: str) -> Path:
    return _artifact_dir(data_root, artifact_id, create=False) / "artifact.json"


def _source_suffix(kind: str) -> str:
    return {
        "html": ".html",
        "markdown": ".md",
        "json": ".json",
        "javascript": ".js",
        "css": ".css",
    }.get(str(kind or "").strip().lower(), ".txt")


def _source_path(data_root: Path, artifact_id: str, kind: str) -> Path:
    return _artifact_dir(data_root, artifact_id) / f"source{_source_suffix(kind)}"


def _default_entry_file(kind: str) -> str:
    return {
        "html": "index.html",
        "markdown": "README.md",
        "json": "data.json",
        "javascript": "main.js",
        "css": "styles.css",
    }.get(str(kind or "").strip().lower(), f"source{_source_suffix(kind)}")


def _normalize_relative_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    parts: List[str] = []
    for part in PurePosixPath(raw).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path traversal is not allowed")
        parts.append(part)
    return "/".join(parts)


def _project_root_name(manifest: Dict[str, Any]) -> str:
    try:
        return _normalize_relative_path(str(manifest.get("project_root") or ""))
    except ValueError:
        return ""


def _artifact_project_root(data_root: Path, artifact_id: str, manifest: Dict[str, Any], *, create: bool = False) -> Path:
    base = _artifact_dir(data_root, artifact_id, create=create)
    root_name = _project_root_name(manifest)
    if not root_name:
        return base
    target = base.joinpath(*root_name.split("/"))
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _entry_file(data_root: Path, artifact_id: str, manifest: Dict[str, Any]) -> str:
    stored = str(manifest.get("entry_file") or "")
    try:
        normalized = _normalize_relative_path(stored)
    except ValueError:
        normalized = ""
    if normalized:
        return normalized
    source_path = Path(manifest.get("source_path") or _source_path(data_root, artifact_id, str(manifest.get("type") or "html")))
    project_root = _artifact_project_root(data_root, artifact_id, manifest, create=False)
    try:
        return source_path.resolve().relative_to(project_root.resolve()).as_posix()
    except Exception:
        return source_path.name


def _resolve_project_path(data_root: Path, artifact_id: str, manifest: Dict[str, Any], relative_path: str, *, create_parent: bool = False) -> Path:
    project_root = _artifact_project_root(data_root, artifact_id, manifest, create=create_parent)
    rel = _normalize_relative_path(relative_path)
    if not rel:
        raise ValueError("path is required")
    target = project_root.joinpath(*rel.split("/"))
    resolved = target.resolve()
    if project_root.resolve() not in [resolved, *resolved.parents]:
        raise ValueError("path escapes artifact root")
    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _collect_artifact_files(data_root: Path, artifact_id: str, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    project_root = _artifact_project_root(data_root, artifact_id, manifest, create=False)
    if not project_root.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for child in sorted(project_root.rglob("*"), key=lambda item: (not item.is_dir(), item.as_posix().lower())):
        if child.name == "artifact.json":
            continue
        try:
            rel = child.relative_to(project_root).as_posix()
            stat = child.stat()
        except Exception:
            continue
        entries.append({
            "path": rel,
            "type": "dir" if child.is_dir() else "file",
            "size": 0 if child.is_dir() else int(stat.st_size),
            "mtime": float(stat.st_mtime),
        })
    return entries


def _safe_json_load(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_id(project_id: str, kind: str, source: str) -> str:
    digest = hashlib.sha256(f"{project_id}:{kind}:{source}".encode("utf-8")).hexdigest()
    return f"art_{digest[:20]}"


def _source_hash(source: str) -> str:
    return hashlib.sha256(str(source or "").encode("utf-8")).hexdigest()


def _is_generic_title(title: str) -> bool:
    value = re.sub(r"\s+", " ", str(title or "").strip().lower())
    return value in {"", "preview", "artifact", "artifact studio", "html preview", "html artifact", "untitled artifact"}


def _plain_text_fragment(value: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fallback_title(kind: str) -> str:
    label = {
        "html": "HTML",
        "markdown": "Markdown",
        "json": "JSON",
        "javascript": "JavaScript",
        "css": "CSS",
        "line-chart": "Line chart",
        "bar-chart": "Bar chart",
        "table": "Table",
        "text": "Text",
    }.get(str(kind or "").strip().lower(), str(kind or "artifact").strip().replace("-", " ").replace("_", " ").title() or "Artifact")
    return f"{label} artifact"


def _infer_title(kind: str, source: str) -> str:
    kind_value = str(kind or "text").strip().lower() or "text"
    source_text = str(source or "")
    if kind_value == "html":
        for pattern in (r"(?is)<title[^>]*>(.*?)</title>", r"(?is)<h1[^>]*>(.*?)</h1>", r"(?is)<h2[^>]*>(.*?)</h2>"):
            match = re.search(pattern, source_text)
            if not match:
                continue
            candidate = _plain_text_fragment(match.group(1))[:120]
            if candidate:
                return candidate
    if kind_value == "markdown":
        for line in source_text.splitlines():
            stripped = str(line or "").strip()
            if stripped.startswith("#"):
                candidate = re.sub(r"^#+\s*", "", stripped).strip()[:120]
                if candidate:
                    return candidate
    return _fallback_title(kind_value)


def _normalize_title(title: str, kind: str, source: str, artifact_id: str) -> str:
    value = str(title or "").strip()
    if value and not _is_generic_title(value):
        return value
    inferred = _infer_title(kind, source)
    return inferred or str(artifact_id or "artifact")


def _record_detail(data_root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    artifact_id = str(manifest.get("artifact_id") or "").strip()
    if not artifact_id:
        return {}
    source_path = Path(manifest.get("source_path") or _source_path(data_root, artifact_id, str(manifest.get("type") or "html")))
    source = ""
    if source_path.exists():
        source = source_path.read_text(encoding="utf-8", errors="replace")
    title = str(manifest.get("title") or artifact_id)
    kind = str(manifest.get("type") or "html")
    entry_file = _entry_file(data_root, artifact_id, manifest)
    files = _collect_artifact_files(data_root, artifact_id, manifest)
    return {
        "artifact_id": artifact_id,
        "project_id": str(manifest.get("project_id") or ""),
        "title": title,
        "display_title": _normalize_title(title, kind, source, artifact_id),
        "type": kind,
        "project_root": _project_root_name(manifest),
        "entry_file": entry_file,
        "files": files,
        "file_count": len([item for item in files if item.get("type") == "file"]),
        "created_at": float(manifest.get("created_at") or 0),
        "updated_at": float(manifest.get("updated_at") or 0),
        "last_seen_at": float(manifest.get("last_seen_at") or 0),
        "source_hash": str(manifest.get("source_hash") or ""),
        "source_path": str(source_path),
        "directory": str(_artifact_dir(data_root, artifact_id, create=False)),
        "source": source,
    }


def remember_artifact(data_root: Path, project_id: str, title: str, kind: str, source: str) -> Dict[str, Any]:
    kind = str(kind or "html").strip().lower() or "html"
    source = str(source or "")
    artifact_id = _artifact_id(project_id, kind, source)
    now = time.time()
    normalized_title = _normalize_title(title, kind, source, artifact_id)
    manifest_path = _manifest_path(data_root, artifact_id)
    manifest = _safe_json_load(manifest_path) if manifest_path.exists() else {}
    if not manifest:
        entry_file = _default_entry_file(kind)
        manifest = {"project_root": "files", "entry_file": entry_file}
        source_path = _artifact_project_root(data_root, artifact_id, manifest, create=True) / entry_file
        write_text_atomically(source_path, source)
        manifest = {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "title": normalized_title,
            "type": kind,
            "project_root": "files",
            "entry_file": entry_file,
            "created_at": now,
            "updated_at": now,
            "last_seen_at": now,
            "source_hash": _source_hash(source),
            "source_path": str(source_path),
        }
        write_text_atomically(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
        return _record_detail(data_root, manifest)
    manifest["last_seen_at"] = now
    if normalized_title and str(manifest.get("title") or "") != normalized_title:
        manifest["title"] = normalized_title
        manifest["updated_at"] = now
    elif not manifest.get("title") or _is_generic_title(str(manifest.get("title") or "")):
        manifest["title"] = normalized_title
    write_text_atomically(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    return _record_detail(data_root, manifest)


def delete_artifact(data_root: Path, artifact_id: str) -> bool:
    manifest_path = _manifest_path(data_root, artifact_id)
    if not manifest_path.exists():
        return False
    target = _artifact_dir(data_root, artifact_id, create=False)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def load_artifact(data_root: Path, artifact_id: str) -> Dict[str, Any]:
    manifest_path = _manifest_path(data_root, artifact_id)
    if not manifest_path.exists():
        return {}
    manifest = _safe_json_load(manifest_path)
    if not manifest:
        return {}
    return _record_detail(data_root, manifest)


def update_artifact(data_root: Path, artifact_id: str, *, title: str | None = None, source: str | None = None) -> Dict[str, Any]:
    manifest_path = _manifest_path(data_root, artifact_id)
    manifest = _safe_json_load(manifest_path)
    if not manifest:
        return {}
    now = time.time()
    kind = str(manifest.get("type") or "html")
    source_path = Path(manifest.get("source_path") or _artifact_project_root(data_root, artifact_id, manifest, create=True) / _entry_file(data_root, artifact_id, manifest))
    current_source = str(source or "") if source is not None else (source_path.read_text(encoding="utf-8", errors="replace") if source_path.exists() else "")
    if title is not None:
        manifest["title"] = _normalize_title(title, kind, current_source, artifact_id)
        manifest["updated_at"] = now
    if source is not None:
        write_text_atomically(source_path, str(source))
        manifest["source_path"] = str(source_path)
        manifest["source_hash"] = _source_hash(str(source))
        manifest["updated_at"] = now
        if not manifest.get("title") or _is_generic_title(str(manifest.get("title") or "")):
            manifest["title"] = _normalize_title(str(manifest.get("title") or artifact_id), kind, str(source), artifact_id)
    manifest["last_seen_at"] = now
    write_text_atomically(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    return _record_detail(data_root, manifest)


def resolve_reusable_artifact(data_root: Path, project_id: str, *, title: str = "", kind: str = "html", source: str = "") -> Dict[str, Any]:
    kind_value = str(kind or "html").strip().lower() or "html"
    title_value = str(title or "").strip()
    source_value = str(source or "")
    artifacts = list_artifacts(data_root, project_id=project_id)
    source_hash = _source_hash(source_value) if source_value else ""
    if source_hash:
        for artifact in artifacts:
            if str(artifact.get("type") or "").strip().lower() != kind_value:
                continue
            if str(artifact.get("source_hash") or "") != source_hash:
                continue
            return {"match": "source_hash", "artifact": artifact}
    if not title_value or _is_generic_title(title_value):
        return {"match": "", "artifact": None}
    normalized_title = re.sub(r"\s+", " ", title_value.strip().lower())
    title_matches: List[Dict[str, Any]] = []
    for artifact in artifacts:
        if str(artifact.get("type") or "").strip().lower() != kind_value:
            continue
        candidate = re.sub(r"\s+", " ", str(artifact.get("title") or artifact.get("display_title") or "").strip().lower())
        if candidate == normalized_title:
            title_matches.append(artifact)
    if len(title_matches) == 1:
        return {"match": "title_unique", "artifact": title_matches[0]}
    return {"match": "", "artifact": None}


def list_artifact_project_files(data_root: Path, artifact_id: str) -> List[Dict[str, Any]]:
    manifest = _safe_json_load(_manifest_path(data_root, artifact_id))
    if not manifest:
        return []
    return _collect_artifact_files(data_root, artifact_id, manifest)


def read_artifact_project_file(data_root: Path, artifact_id: str, relative_path: str) -> Dict[str, Any]:
    manifest = _safe_json_load(_manifest_path(data_root, artifact_id))
    if not manifest:
        return {}
    rel = _normalize_relative_path(relative_path) or _entry_file(data_root, artifact_id, manifest)
    target = _resolve_project_path(data_root, artifact_id, manifest, rel)
    if not target.exists() or not target.is_file():
        return {}
    return {
        "path": rel,
        "content": target.read_text(encoding="utf-8", errors="replace"),
    }


def write_artifact_project_file(data_root: Path, artifact_id: str, relative_path: str, content: str) -> Dict[str, Any]:
    manifest_path = _manifest_path(data_root, artifact_id)
    manifest = _safe_json_load(manifest_path)
    if not manifest:
        return {}
    now = time.time()
    rel = _normalize_relative_path(relative_path) or _entry_file(data_root, artifact_id, manifest)
    target = _resolve_project_path(data_root, artifact_id, manifest, rel, create_parent=True)
    write_text_atomically(target, str(content))
    manifest["source_path"] = str(target) if rel == _entry_file(data_root, artifact_id, manifest) else str(manifest.get("source_path") or target)
    manifest["source_hash"] = _source_hash(str(content)) if rel == _entry_file(data_root, artifact_id, manifest) else str(manifest.get("source_hash") or "")
    if not manifest.get("entry_file"):
        manifest["entry_file"] = _entry_file(data_root, artifact_id, manifest)
    if rel == _entry_file(data_root, artifact_id, manifest) and (not manifest.get("title") or _is_generic_title(str(manifest.get("title") or ""))):
        manifest["title"] = _normalize_title(str(manifest.get("title") or artifact_id), str(manifest.get("type") or "html"), str(content), artifact_id)
    manifest["updated_at"] = now
    manifest["last_seen_at"] = now
    write_text_atomically(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    return _record_detail(data_root, manifest)


def create_artifact_project_folder(data_root: Path, artifact_id: str, relative_path: str) -> Dict[str, Any]:
    manifest_path = _manifest_path(data_root, artifact_id)
    manifest = _safe_json_load(manifest_path)
    if not manifest:
        return {}
    now = time.time()
    target = _resolve_project_path(data_root, artifact_id, manifest, relative_path, create_parent=True)
    target.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = now
    manifest["last_seen_at"] = now
    write_text_atomically(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    return _record_detail(data_root, manifest)


def resolve_artifact_preview_file(data_root: Path, artifact_id: str, relative_path: str = "") -> Path:
    manifest = _safe_json_load(_manifest_path(data_root, artifact_id))
    if not manifest:
        return Path()
    rel = _normalize_relative_path(relative_path) or _entry_file(data_root, artifact_id, manifest)
    try:
        return _resolve_project_path(data_root, artifact_id, manifest, rel)
    except ValueError:
        return Path()


def list_artifacts(data_root: Path, project_id: str = "") -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    root = artifact_library_dir(data_root)
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        manifest = _safe_json_load(child / "artifact.json")
        if not manifest:
            continue
        if project_id and str(manifest.get("project_id") or "") != str(project_id):
            continue
        detail = _record_detail(data_root, manifest)
        if not detail:
            continue
        detail.pop("source", None)
        items.append(detail)
    items.sort(key=lambda item: (-(item.get("updated_at") or 0), item.get("title") or ""))
    return items
