"""File-system browse endpoint — backs the directory picker UI.

Returns directory listings (dirs first, files alphabetically) with size + mtime.
Cross-platform: handles Windows drive letters, UNC, and POSIX paths.

Also exposes the **default workspace directory** (``<project_root>/workspace``
or ``<data_dir>/workspace`` depending on how Blackboard is launched) so the
UI picker can land there by default unless the user picks something else.

Favorites are persisted server-side at ``data/ui/picker_favorites.json`` so
they survive across browsers and across users on the same machine.
"""
from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("api.files")

router = APIRouter(prefix="/api/files", tags=["files"])
_MAX_TEXT_FILE_BYTES = 512 * 1024

# Repo root (parent of the ``blackboard`` package). Used to compute the default
# workspace path when no ``BLACKBOARD_DATA_DIR`` override is set.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _list_drives_windows() -> List[Dict[str, Any]]:
    drives: List[Dict[str, Any]] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append({
                "name": f"{letter}:",
                "path": root,
                "type": "drive",
                "size": 0,
                "mtime": 0,
            })
    return drives


def _resolve_path(directory: str) -> Path:
    """Resolve user-supplied path safely (no symlink trickery for ~ expansion)."""
    if not directory or directory.strip() in ("", "/", "\\"):
        # Empty path on Windows → return synthetic 'My Computer'-style drive list
        return Path("__drives__")
    expanded = os.path.expanduser(directory.strip())
    return Path(expanded).resolve()


def _resolve_file_path(path: str) -> Path:
    value = (path or "").strip()
    if not value:
        raise HTTPException(400, "path required")
    return Path(os.path.expanduser(value)).resolve()


@router.get("/list")
async def list_dir(
    directory: str = "",
    show_hidden: bool = False,
    only_dirs: bool = False,
) -> Dict[str, Any]:
    """List directory contents.

    - ``directory=""`` on Windows returns the drive list.
    - Otherwise returns ``{cwd, parent, entries: [{name, path, type, size, mtime}]}``.
    """
    p = _resolve_path(directory)

    if str(p) == "__drives__" and os.name == "nt":
        return {
            "cwd": "",
            "parent": None,
            "entries": _list_drives_windows(),
            "is_drive_list": True,
        }

    if not p.exists():
        raise HTTPException(404, f"path not found: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")

    entries: List[Dict[str, Any]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            name = child.name
            if not show_hidden and name.startswith("."):
                continue
            if not show_hidden and os.name == "nt":
                # Cheap heuristic: skip Windows system-y names if not explicitly asked.
                if name.lower() in {"$recycle.bin", "system volume information", "pagefile.sys", "hiberfil.sys"}:
                    continue
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except (PermissionError, OSError):
                continue
            if only_dirs and not is_dir:
                continue
            entries.append({
                "name": name,
                "path": str(child),
                "type": "dir" if is_dir else "file",
                "size": int(stat.st_size) if not is_dir else 0,
                "mtime": float(stat.st_mtime),
            })
    except PermissionError as exc:
        raise HTTPException(403, f"permission denied: {exc}") from exc

    parent: Optional[str] = None
    try:
        if p.parent != p:
            parent = str(p.parent)
    except Exception:
        parent = None

    return {
        "cwd": str(p),
        "parent": parent,
        "entries": entries,
        "is_drive_list": False,
    }


# ── Workspace + favorites helpers ───────────────────────────────────


def _data_root_from_request(request: Request) -> Path:
    """Best-effort fetch of the configured data root (set in main.py lifespan)."""
    root = getattr(request.app.state, "data_root", None)
    if root is None:
        root = Path(os.environ.get("BLACKBOARD_DATA_DIR", "data"))
    return Path(root).resolve()


def resolve_workspace_dir(data_root: Optional[Path] = None) -> Path:
    """Return the canonical workspace path.

    Preference order:
      1. ``BLACKBOARD_WORKSPACE_DIR`` env override (absolute path)
      2. ``<repo_root>/workspace`` if the repo layout exists (development)
      3. ``<data_root>/workspace`` (fallback for installed/packaged layouts)
    """
    env_override = os.environ.get("BLACKBOARD_WORKSPACE_DIR", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    repo_workspace = _REPO_ROOT / "workspace"
    if repo_workspace.exists():
        return repo_workspace.resolve()
    if data_root is not None:
        return (Path(data_root) / "workspace").resolve()
    return repo_workspace.resolve()


def ensure_workspace_dir(data_root: Optional[Path] = None) -> Path:
    """Make sure the workspace dir exists; return its absolute path."""
    target = resolve_workspace_dir(data_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _favorites_path(data_root: Path) -> Path:
    return data_root / "ui" / "picker_favorites.json"


def _load_favorites(data_root: Path) -> List[Dict[str, str]]:
    p = _favorites_path(data_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out: List[Dict[str, str]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            label = str(entry.get("label") or "").strip() or Path(path).name or path
            out.append({"label": label, "path": path})
        return out
    except Exception as exc:
        logger.warning("[files] favorites load failed: %s", exc)
        return []


def _save_favorites(data_root: Path, favorites: List[Dict[str, str]]) -> None:
    p = _favorites_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(p, json.dumps(favorites, indent=2))


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/home")
async def home(request: Request) -> Dict[str, Any]:
    """Return useful starting paths plus the default workspace directory.

    The picker uses ``default_workspace`` as its initial cwd unless the caller
    or persisted state specifies otherwise. ``shortcuts[0]`` is always the
    workspace so it appears at the top of the sidebar.
    """
    data_root = _data_root_from_request(request)
    workspace = ensure_workspace_dir(data_root)
    paths: List[Dict[str, str]] = [{"label": "Workspace", "path": str(workspace), "kind": "workspace"}]
    for label, candidate in [
        ("Home", str(Path.home())),
        ("Desktop", str(Path.home() / "Desktop")),
        ("Documents", str(Path.home() / "Documents")),
    ]:
        if Path(candidate).exists():
            paths.append({"label": label, "path": candidate})
    if os.name == "nt":
        for letter in ("C", "D", "E"):
            root = f"{letter}:\\"
            if Path(root).exists():
                paths.append({"label": f"{letter}: drive", "path": root})
    return {
        "shortcuts": paths,
        "default_workspace": str(workspace),
        "favorites": _load_favorites(data_root),
    }


class MkdirBody(BaseModel):
    parent: str
    name: str


@router.post("/mkdir")
async def mkdir(body: MkdirBody) -> Dict[str, Any]:
    """Create a new sub-directory inside ``parent``. Used by the picker's
    “+ New folder” button. Rejects path traversal (``..``) and absolute
    ``name`` values.
    """
    parent = (body.parent or "").strip()
    name = (body.name or "").strip()
    if not parent or not name:
        raise HTTPException(400, "parent and name are required")
    if any(sep in name for sep in ("/", "\\")) or name in (".", ".."):
        raise HTTPException(400, "name must be a single path segment")
    parent_path = Path(parent).expanduser().resolve()
    if not parent_path.exists() or not parent_path.is_dir():
        raise HTTPException(404, f"parent not found: {parent_path}")
    target = (parent_path / name).resolve()
    # Ensure the new path stays under the parent (defense-in-depth).
    try:
        target.relative_to(parent_path)
    except ValueError:
        raise HTTPException(400, "resolved path escapes parent")
    if target.exists():
        raise HTTPException(409, "already exists")
    try:
        target.mkdir(parents=False, exist_ok=False)
    except PermissionError as exc:
        raise HTTPException(403, f"permission denied: {exc}") from exc
    except OSError as exc:
        raise HTTPException(400, f"mkdir failed: {exc}") from exc
    return {"path": str(target), "name": name}


class FavoriteBody(BaseModel):
    path: str
    label: str = ""


class WriteFileBody(BaseModel):
    path: str
    content: str = ""


@router.get("/favorites")
async def list_favorites(request: Request) -> Dict[str, Any]:
    return {"favorites": _load_favorites(_data_root_from_request(request))}


@router.post("/favorites")
async def add_favorite(body: FavoriteBody, request: Request) -> Dict[str, Any]:
    path = (body.path or "").strip()
    if not path:
        raise HTTPException(400, "path required")
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(404, f"directory not found: {resolved}")
    data_root = _data_root_from_request(request)
    favorites = _load_favorites(data_root)
    canonical = str(resolved)
    favorites = [f for f in favorites if f["path"] != canonical]
    favorites.insert(0, {
        "label": (body.label or "").strip() or resolved.name or canonical,
        "path": canonical,
    })
    favorites = favorites[:24]  # cap to keep the sidebar tidy
    _save_favorites(data_root, favorites)
    return {"favorites": favorites}


@router.get("/read")
async def read_text_file(path: str) -> Dict[str, Any]:
    target = _resolve_file_path(path)
    if not target.exists():
        raise HTTPException(404, f"file not found: {target}")
    if target.is_dir():
        raise HTTPException(400, f"path is a directory: {target}")
    try:
        stat = target.stat()
    except OSError as exc:
        raise HTTPException(400, f"stat failed: {exc}") from exc
    if stat.st_size > _MAX_TEXT_FILE_BYTES:
        raise HTTPException(413, f"file too large: {stat.st_size} bytes")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(400, f"read failed: {exc}") from exc
    return {
        "path": str(target),
        "name": target.name,
        "content": content,
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
    }


@router.post("/write")
async def write_text_file(body: WriteFileBody) -> Dict[str, Any]:
    target = _resolve_file_path(body.path)
    if target.exists() and target.is_dir():
        raise HTTPException(400, f"path is a directory: {target}")
    parent = target.parent
    if not parent.exists() or not parent.is_dir():
        raise HTTPException(404, f"parent directory not found: {parent}")
    try:
        write_text_atomically(target, str(body.content or ""))
        stat = target.stat()
    except OSError as exc:
        raise HTTPException(400, f"write failed: {exc}") from exc
    return {
        "path": str(target),
        "name": target.name,
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
    }


@router.delete("/favorites")
async def remove_favorite(path: str, request: Request) -> Dict[str, Any]:
    target = (path or "").strip()
    if not target:
        raise HTTPException(400, "path required")
    data_root = _data_root_from_request(request)
    favorites = _load_favorites(data_root)
    canonical = str(Path(target).expanduser().resolve())
    new_favs = [f for f in favorites if f["path"] not in (target, canonical)]
    _save_favorites(data_root, new_favs)
    return {"favorites": new_favs}


@router.get("/probe")
async def probe(directory: str) -> Dict[str, Any]:
    """Cheap existence/type check used by the picker to validate user input
    before navigating (so we can show “not found” inline rather than throwing).
    """
    if not directory.strip():
        return {"exists": False, "is_dir": False}
    p = Path(directory).expanduser().resolve()
    return {"exists": p.exists(), "is_dir": p.is_dir(), "path": str(p)}
