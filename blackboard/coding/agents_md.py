"""AGENTS.md loader — discovers + caches AGENTS.md files. Layered: home -> repo root -> cwd.

Direct port of luna/workers/coding/agents_md.py.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from blackboard.kernel.logger import get_logger

logger = get_logger("coding.agents_md")

_FILENAME = "AGENTS.md"
_CACHE_TTL = 60.0


class AgentsMDLoader:
    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[str, float, float]] = {}

    def load(self, cwd: str = ".", explicit_path: Optional[str] = None) -> str:
        if explicit_path:
            return self._read(Path(explicit_path))
        paths = self._discover_paths(Path(cwd).resolve())
        if not paths:
            return ""
        merged = "\n\n".join(self._read(p) for p in paths)
        return merged.strip()

    def inspect(self, cwd: str = ".", explicit_path: Optional[str] = None, *, include_content: bool = False) -> Dict[str, Any]:
        resolved_cwd = Path(cwd).resolve()
        if explicit_path:
            target = Path(explicit_path).resolve()
            chain = [self._describe_path(target, repo_root=None, resolved_cwd=resolved_cwd, include_content=include_content, explicit=True)]
        else:
            repo_root = self._find_repo_root(resolved_cwd)
            chain = [
                self._describe_path(path, repo_root=repo_root, resolved_cwd=resolved_cwd, include_content=include_content)
                for path in self._discover_paths(resolved_cwd)
            ]
        merged = "\n\n".join(str(item.get("content") or "") for item in chain if item.get("content")) if include_content else ""
        return {
            "cwd": str(resolved_cwd),
            "explicit_path": str(Path(explicit_path).resolve()) if explicit_path else "",
            "found": bool(chain),
            "chain": chain,
            "merged_content": merged.strip(),
            "generated_at": time.time(),
        }

    def discover_paths(self, cwd: str = ".", explicit_path: Optional[str] = None) -> List[str]:
        if explicit_path:
            return [str(Path(explicit_path).resolve())]
        return [str(path.resolve()) for path in self._discover_paths(Path(cwd).resolve())]

    def _find_repo_root(self, cwd: Path) -> Optional[Path]:
        current = cwd
        for _ in range(20):
            if (current / ".git").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def _discover_paths(self, cwd: Path) -> List[Path]:
        discovered: List[Path] = []
        ancestry: List[Path] = [cwd]
        current = cwd
        repo_root = self._find_repo_root(cwd)
        for _ in range(20):
            parent = current.parent
            if parent == current:
                break
            current = parent
            ancestry.append(current)
            if repo_root is not None and current == repo_root:
                break
        home_candidate = Path.home() / _FILENAME
        if home_candidate.exists():
            discovered.append(home_candidate)
        chain = list(reversed(ancestry))
        if repo_root is not None:
            try:
                idx = chain.index(repo_root)
                chain = chain[idx:]
            except ValueError:
                chain = [repo_root]
        for directory in chain:
            candidate = directory / _FILENAME
            if candidate.exists():
                discovered.append(candidate)
        unique: List[Path] = []
        seen: set[str] = set()
        for path in discovered:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _describe_path(
        self,
        path: Path,
        *,
        repo_root: Optional[Path],
        resolved_cwd: Path,
        include_content: bool,
        explicit: bool = False,
    ) -> Dict[str, Any]:
        resolved = path.resolve()
        scope = "subdir"
        if explicit:
            scope = "explicit"
        elif resolved == (Path.home() / _FILENAME).resolve():
            scope = "home"
        elif repo_root is not None and resolved == (repo_root / _FILENAME).resolve():
            scope = "repo_root"
        elif resolved.parent == resolved_cwd:
            scope = "cwd"
        try:
            stat = resolved.stat()
            exists = True
            size = int(stat.st_size)
            mtime = float(stat.st_mtime)
        except Exception:
            exists = False
            size = 0
            mtime = 0.0
        payload: Dict[str, Any] = {
            "path": str(resolved),
            "scope": scope,
            "exists": exists,
            "size": size,
            "mtime": mtime,
        }
        if include_content:
            payload["content"] = self._read(resolved)
        return payload

    def _read(self, path: Path) -> str:
        key = str(path.resolve())
        now = time.time()
        try:
            current_mtime = float(path.stat().st_mtime)
        except Exception as exc:
            self._cache.pop(key, None)
            logger.debug("[agents_md] read failed for %s: %s", path, exc)
            return ""
        cached = self._cache.get(key)
        if cached is not None:
            content, checked, cached_mtime = cached
            if cached_mtime == current_mtime and now - checked < _CACHE_TTL:
                return content
        try:
            content = path.read_text(encoding="utf-8").strip()
            self._cache[key] = (content, now, current_mtime)
            return content
        except Exception as exc:
            logger.debug("[agents_md] read failed for %s: %s", path, exc)
            return ""

    def invalidate(self) -> None:
        self._cache.clear()


_loader: Optional[AgentsMDLoader] = None


def get_loader() -> AgentsMDLoader:
    global _loader
    if _loader is None:
        _loader = AgentsMDLoader()
    return _loader


def load_agents_md(cwd: str = ".", explicit_path: Optional[str] = None) -> str:
    return get_loader().load(cwd=cwd, explicit_path=explicit_path)


def inspect_agents_md(cwd: str = ".", explicit_path: Optional[str] = None, *, include_content: bool = False) -> Dict[str, Any]:
    return get_loader().inspect(cwd=cwd, explicit_path=explicit_path, include_content=include_content)
