"""SKILL.md progressive-disclosure loader.

Scans ``data/skills/`` (global) and per-project ``.skills/`` for SKILL.md files.
Indexes (name, description, path); loads full body only on ``skill_invoke``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from blackboard.kernel.logger import get_logger
from blackboard.react.tool_registry import ToolRegistry

logger = get_logger("coding.skills")

_NAME_RE = re.compile(r"^name\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_DESC_RE = re.compile(r"^description\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_TAGS_RE = re.compile(r"^tags\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_PRIORITY_RE = re.compile(r"^priority\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class SkillIndexEntry:
    name: str
    description: str
    path: str
    source: str = "global"   # "global" | "project"
    tags: List[str] = field(default_factory=list)
    priority: int = 0
    when_to_use: str = ""
    composes: List[str] = field(default_factory=list)
    generated: bool = False


@dataclass
class SkillIndex:
    entries: Dict[str, SkillIndexEntry] = field(default_factory=dict)

    def add(self, entry: SkillIndexEntry) -> None:
        self.entries[entry.name] = entry

    def list(self) -> List[SkillIndexEntry]:
        return sorted(self.entries.values(), key=lambda entry: (-int(entry.priority), entry.name))

    def get(self, name: str) -> Optional[SkillIndexEntry]:
        return self.entries.get(name)

    def as_context_block(self, *, max_entries: int = 12) -> str:
        if not self.entries:
            return ""
        items = self.list()[:max_entries]
        lines = ["<skills>"]
        lines.append("Available skills (call `skill_invoke` with name to load the full body):")
        for entry in items:
            tags = f" tags={','.join(entry.tags[:5])}" if entry.tags else ""
            composes = f" composes={','.join(entry.composes[:4])}" if entry.composes else ""
            generated = " generated=true" if entry.generated else ""
            suffix = f" when={entry.when_to_use[:100]}" if entry.when_to_use else ""
            lines.append(f"- {entry.name} ({entry.source}, priority={entry.priority}{tags}{composes}{generated}): {entry.description[:140]}{suffix}")
        lines.append("</skills>")
        return "\n".join(lines)


def _parse_skill_md(path: Path) -> Optional[SkillIndexEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    name = ""
    description = ""
    tags: List[str] = []
    priority = 0
    when_to_use = ""
    composes: List[str] = []
    generated = False
    head = "\n".join(text.splitlines()[:30])
    if text.startswith("---"):
        end = text.find("---", 3)
        if end >= 0:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
                if isinstance(meta, dict):
                    name = str(meta.get("name") or "").strip().strip('"').strip("'")
                    description = str(meta.get("description") or "").strip().strip('"').strip("'")
                    raw_tags = meta.get("tags") or []
                    if isinstance(raw_tags, str):
                        tags = [item.strip() for item in raw_tags.strip("[]").split(",") if item.strip()]
                    elif isinstance(raw_tags, list):
                        tags = [str(item).strip() for item in raw_tags if str(item).strip()]
                    when_to_use = str(meta.get("when_to_use") or "").strip().strip('"').strip("'")
                    raw_composes = meta.get("composes") or []
                    if isinstance(raw_composes, str):
                        composes = [item.strip() for item in raw_composes.strip("[]").split(",") if item.strip()]
                    elif isinstance(raw_composes, list):
                        composes = [str(item).strip() for item in raw_composes if str(item).strip()]
                    generated = bool(meta.get("generated", False))
                    try:
                        priority = int(meta.get("priority") or 0)
                    except Exception:
                        priority = 0
            except Exception:
                pass
    name_match = _NAME_RE.search(head)
    desc_match = _DESC_RE.search(head)
    tags_match = _TAGS_RE.search(head)
    priority_match = _PRIORITY_RE.search(head)
    if not name and name_match:
        name = name_match.group(1).strip().strip('"').strip("'")
    if not description and desc_match:
        description = desc_match.group(1).strip().strip('"').strip("'")
    if not tags and tags_match:
        tags = [item.strip().strip('"').strip("'") for item in tags_match.group(1).strip().strip("[]").split(",") if item.strip()]
    if not priority and priority_match:
        try:
            priority = int(priority_match.group(1).strip())
        except Exception:
            priority = 0
    if not name:
        # Fall back to filename: foo/SKILL.md -> 'foo'
        if path.name.lower() == "skill.md" and path.parent.name:
            name = path.parent.name
        else:
            name = path.stem
    return SkillIndexEntry(
        name=name,
        description=description or "(no description)",
        path=str(path),
        tags=tags,
        priority=priority,
        when_to_use=when_to_use,
        composes=composes,
        generated=generated,
    )


def build_skill_index(*, global_dir: Optional[Path] = None, project_dirs: Optional[List[Path]] = None) -> SkillIndex:
    index = SkillIndex()
    if global_dir and global_dir.exists():
        for path in sorted(global_dir.rglob("SKILL.md")):
            entry = _parse_skill_md(path)
            if entry:
                entry.source = "global"
                index.add(entry)
    for pdir in project_dirs or []:
        if not pdir.exists():
            continue
        for path in sorted(pdir.rglob("SKILL.md")):
            entry = _parse_skill_md(path)
            if entry:
                parts = {part.lower() for part in path.parts}
                if "adaptive_skills" in parts:
                    source_name = "adaptive"
                elif "promoted_skills" in parts:
                    source_name = "promoted"
                else:
                    source_name = "project"
                entry.source = source_name
                index.add(entry)
    return index


def load_skill_body(index: SkillIndex, name: str) -> str:
    entry = index.get(name)
    if entry is None:
        return ""
    try:
        return Path(entry.path).read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("[skills] failed to read %s: %s", entry.path, exc)
        return ""


# ── ReAct tool ───────────────────────────────────────────────────


_active_index: Optional[SkillIndex] = None


def set_active_index(index: SkillIndex) -> None:
    global _active_index
    _active_index = index


def get_active_index() -> SkillIndex:
    return _active_index or SkillIndex()


def _skill_invoke(args):
    name = str((args or {}).get("name") or "")
    if not name:
        return '{"error": "name required"}'
    runtime = dict((args or {}).get("_tool_runtime") or {})
    index = runtime.get("skill_index") or get_active_index()
    body = load_skill_body(index, name)
    if not body:
        return f'{{"error": "skill not found: {name}"}}'
    import json
    return json.dumps({"name": name, "body": body})


def register_skill_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "skill_invoke",
        "Load the full body of a SKILL.md skill by name (progressive disclosure).",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        _skill_invoke,
        tags=["skill", "read"],
    )
