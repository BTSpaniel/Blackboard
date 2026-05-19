"""Project — the top-level workspace record. Stored at data/projects/<id>/project.json."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.project")


@dataclass
class Project:
    project_id: str
    name: str
    root: str
    active_branch: str = "main"
    providers: Dict[str, str] = field(default_factory=dict)
    execution: Dict[str, str] = field(default_factory=lambda: {"terminal": "local", "preview": "python", "tester": "playwright"})
    secrets: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.setdefault("id", self.project_id)
        return payload


def _slugify(name: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return out or uuid.uuid4().hex[:8]


class ProjectStore:
    """File-backed project CRUD."""

    def __init__(self, data_root: Path) -> None:
        self._root = Path(data_root)
        (self._root / "projects").mkdir(parents=True, exist_ok=True)
        self._state_path = self._root / "projects" / "active.json"

    def _project_path(self, project_id: str) -> Path:
        return self._root / "projects" / project_id / "project.json"

    def list(self) -> List[Project]:
        out: List[Project] = []
        for entry in sorted((self._root / "projects").iterdir()):
            if not entry.is_dir():
                continue
            project = self._load_path(entry / "project.json")
            if project:
                out.append(project)
        return out

    def _load_path(self, path: Path) -> Optional[Project]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Project(**{k: v for k, v in data.items() if k in Project.__dataclass_fields__})
        except Exception as exc:
            logger.warning("Failed to read project %s: %s", path, exc)
            return None

    def get(self, project_id: str) -> Optional[Project]:
        return self._load_path(self._project_path(project_id))

    def save(self, project: Project) -> Project:
        project.updated_at = time.time()
        path = self._project_path(project.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path, json.dumps(project.to_dict(), indent=2))
        try:
            from blackboard.workspace.version_control import commit_safely
            commit_safely(
                f"project: create {project.project_id} ({project.name})",
                kind="vcs.project_create",
                paths=[str(path)],
            )
        except Exception:
            pass
        return project

    def create(
        self,
        *,
        name: str,
        root: str,
        active_branch: str = "main",
        providers: Optional[Dict[str, str]] = None,
        execution: Optional[Dict[str, str]] = None,
        secrets: Optional[Dict[str, str]] = None,
    ) -> Project:
        project_id = _slugify(name)
        if self.get(project_id) is not None:
            project_id = f"{project_id}-{uuid.uuid4().hex[:6]}"
        resolved_root = Path(root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        project = Project(
            project_id=project_id,
            name=name,
            root=str(resolved_root),
            active_branch=active_branch,
            providers=dict(providers or {}),
            execution=dict(execution or {"terminal": "local", "preview": "python", "tester": "playwright"}),
            secrets=dict(secrets or {}),
        )
        return self.save(project)

    def delete(self, project_id: str) -> bool:
        path = self._project_path(project_id)
        if not path.exists():
            return False
        # Soft-delete: rename .deleted/<ts>
        try:
            target = self._root / "projects" / f".deleted_{project_id}_{int(time.time())}"
            path.parent.rename(target)
            return True
        except Exception as exc:
            logger.warning("Project delete failed: %s", exc)
            return False

    def get_active(self) -> Optional[str]:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return str(data.get("project_id") or "") or None
        except Exception:
            return None

    def set_active(self, project_id: str) -> None:
        write_text_atomically(self._state_path, json.dumps({"project_id": project_id}, indent=2))
        try:
            from blackboard.workspace.version_control import commit_safely
            commit_safely(
                f"project: switch active → {project_id}",
                kind="vcs.project_switch",
                paths=[str(self._state_path)],
            )
        except Exception:
            pass
