"""Markdown wiki manager for durable Blackboard project knowledge."""
from __future__ import annotations

import re
import threading
import time
import json
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.governors.data_protection import get_data_protection_governor
from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.kernel.json_schema import build_response_format, parse_json_payload, validate_payload


_FRONTMATTER_RE = re.compile(r"^---\n[\s\S]*?\n---\n?", re.MULTILINE)
_WIKI_INGEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 1200},
        "pages": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 240},
                    "content": {"type": "string", "maxLength": 40000},
                },
                "required": ["name", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "pages"],
    "additionalProperties": False,
}


@dataclass
class WikiPage:
    path: Path
    name: str
    content: str
    mtime: float

    @property
    def summary_line(self) -> str:
        text = _FRONTMATTER_RE.sub("", self.content).strip()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:160]
        return ""

    def to_dict(self, root: Path, *, include_content: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "path": str(self.path.relative_to(root)).replace("\\", "/"),
            "summary": self.summary_line,
            "mtime": self.mtime,
        }
        if include_content:
            payload["content"] = self.content
        return payload


class WikiManager:
    def __init__(self, wiki_dir: Path) -> None:
        self.root = Path(wiki_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "raw").mkdir(exist_ok=True)
        self._lock = threading.Lock()
        if not (self.root / "schema.md").exists():
            write_text_atomically(self.root / "schema.md", _DEFAULT_SCHEMA, encoding="utf-8")

    def read_page(self, name: str) -> Optional[str]:
        path = self._page_path(name)
        if not path.exists() or not self._within_root(path):
            return None
        return path.read_text(encoding="utf-8")

    def write_page(self, name: str, content: str, *, source: str = "manual") -> WikiPage:
        safe_name = self._safe_page_name(name)
        path = self._page_path(safe_name)
        now = _iso_now()
        with self._lock:
            created = now
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                match = re.search(r"^created:\s*(\S+)", existing, re.MULTILINE)
                if match:
                    created = match.group(1)
            protection = get_data_protection_governor().protect_text(content, operation="persist_wiki_page")
            body = _ensure_frontmatter(protection.protected, created=created, updated=now, source=source, metadata=protection.metadata())
            write_text_atomically(path, body, encoding="utf-8")
            self._append_log_locked("write", f"{safe_name} | source={source}")
        self.rebuild_index()
        return self._read_page_object(path)

    def store_raw(self, name: str, content: str) -> Path:
        safe_name = self._safe_page_name(name)
        path = (self.root / "raw" / safe_name).with_suffix(".md").resolve()
        if not self._within_root(path):
            raise ValueError("raw path outside wiki root")
        path.parent.mkdir(parents=True, exist_ok=True)
        protection = get_data_protection_governor().protect_text(content, operation="persist_wiki_raw")
        write_text_atomically(path, protection.protected, encoding="utf-8")
        with self._lock:
            self._append_log_locked("raw", safe_name)
        return path

    def delete_page(self, name: str) -> bool:
        path = self._page_path(name)
        if not path.exists() or not self._within_root(path):
            return False
        path.unlink()
        with self._lock:
            self._append_log_locked("delete", name)
        self.rebuild_index()
        return True

    def list_pages(self, subdir: str = "") -> List[WikiPage]:
        base = self.root / subdir if subdir else self.root
        if not self._within_root(base):
            return []
        pages: List[WikiPage] = []
        for path in sorted(base.rglob("*.md")):
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            stem = str(path.relative_to(self.root).with_suffix("")).replace("\\", "/")
            if rel in {"index.md", "log.md", "schema.md"} or rel.startswith("raw/"):
                continue
            pages.append(self._read_page_object(path))
        return pages

    def search(self, query: str, max_results: int = 6) -> List[WikiPage]:
        terms = [term.lower() for term in re.split(r"\W+", query or "") if len(term) > 2]
        if not terms:
            return []
        scored: List[tuple[int, WikiPage]] = []
        for page in self.list_pages():
            haystack = f"{page.name}\n{page.content}".lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append((score, page))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [page for _, page in scored[: max(1, int(max_results or 6))]]

    def context_block(self, query: str, *, max_results: int = 3, per_page_chars: int = 1200) -> str:
        hits = self.search(query, max_results=max_results)
        if not hits:
            return ""
        lines = ["<wiki_context>", "Relevant durable wiki pages:"]
        for page in hits:
            excerpt = _FRONTMATTER_RE.sub("", page.content).strip()[:per_page_chars]
            lines.append(f"\n### [[{page.name}]]")
            lines.append(excerpt)
        lines.append("</wiki_context>")
        return "\n".join(lines)

    async def ingest(self, source_text: str, source_name: str, llm_call, *, save_raw: bool = True) -> Dict[str, Any]:
        if save_raw:
            self.store_raw(source_name, source_text)
        index_text = (self.root / "index.md").read_text(encoding="utf-8") if (self.root / "index.md").exists() else ""
        prompt = textwrap.dedent(f"""
            You are maintaining Blackboard's durable project wiki.

            SOURCE NAME: {source_name}
            SOURCE TEXT:
            {source_text[:8000]}

            EXISTING INDEX:
            {index_text[:3000]}

            Return ONLY valid JSON:
            {{"summary": "...", "pages": [{{"name": "path/PageName", "content": "full markdown content"}}]}}

            Rules:
            - Create pages only for reusable project knowledge, decisions, templates, commands, or API facts.
            - Use [[wikilinks]] for related pages.
            - Do not store secrets or API key values.
        """).strip()
        result = await llm_call(
            prompt,
            max_tokens=4096,
            temperature=0.1,
            response_format=build_response_format(_WIKI_INGEST_SCHEMA, "wiki_ingest"),
        )
        data, parse_error = parse_json_payload(result)
        if parse_error:
            data = {}
        else:
            data, validation_error = validate_payload(data, _WIKI_INGEST_SCHEMA, path="wiki_ingest")
            if validation_error:
                data = {}
        pages_created: List[str] = []
        pages_updated: List[str] = []
        for op in data.get("pages", []) if isinstance(data, dict) else []:
            name = str(op.get("name") or "").strip()
            content = str(op.get("content") or "").strip()
            if not name or not content:
                continue
            exists = self.read_page(name) is not None
            self.write_page(name, content, source=source_name)
            (pages_updated if exists else pages_created).append(name)
        return {
            "summary": data.get("summary", f"Ingested {source_name}") if isinstance(data, dict) else f"Ingested {source_name}",
            "pages_created": pages_created,
            "pages_updated": pages_updated,
        }

    async def query(self, question: str, llm_call, *, file_answer: bool = False) -> str:
        hits = self.search(question, max_results=6)
        page_context = "\n\n".join(f"### [[{page.name}]]\n{page.content[:1800]}" for page in hits)
        if not page_context:
            page_context = "(No relevant wiki pages found.)"
        prompt = textwrap.dedent(f"""
            Answer using ONLY Blackboard wiki content below. Cite pages with [[PageName]] links.
            If the wiki does not contain enough information, say so clearly.

            QUESTION: {question}

            WIKI PAGES:
            {page_context}
        """).strip()
        answer = (await llm_call(prompt, max_tokens=1500, temperature=0.1)).strip()
        if file_answer and answer:
            slug = re.sub(r"[^A-Za-z0-9_. -]", "_", question[:60]).strip(" _.")
            self.write_page(f"queries/{slug or 'answer'}", f"# Q: {question}\n\n{answer}", source="query")
        with self._lock:
            self._append_log_locked("query", question[:100])
        return answer

    async def lint(self, llm_call) -> str:
        health = self.health()
        summaries = "\n".join(f"- [[{page.name}]]: {page.summary_line[:120]}" for page in self.list_pages()[:80])
        prompt = textwrap.dedent(f"""
            Health-check Blackboard's durable project wiki.

            PAGES:
            {summaries}

            DETERMINISTIC HEALTH:
            {json.dumps(health, indent=2, default=str)}

            Return a concise markdown report with actionable fixes for contradictions, stale claims, missing cross-references, and knowledge gaps.
        """).strip()
        report = (await llm_call(prompt, max_tokens=1500, temperature=0.2)).strip()
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.write_page(f"maintenance/lint-{date}", f"# Wiki Lint Report {date}\n\n{report}", source="lint")
        return report

    def health(self) -> Dict[str, Any]:
        pages = self.list_pages()
        names = {page.name for page in pages}
        inbound: Dict[str, int] = {name: 0 for name in names}
        broken_links: List[Dict[str, str]] = []
        pages_without_summary: List[str] = []
        for page in pages:
            if not page.summary_line:
                pages_without_summary.append(page.name)
            for link in re.findall(r"\[\[([^\]|#]+)", page.content):
                target = link.strip()
                if target in inbound:
                    inbound[target] += 1
                else:
                    broken_links.append({"page": page.name, "target": target})
        orphans = [name for name, count in inbound.items() if count == 0]
        duplicate_summaries: Dict[str, List[str]] = {}
        for page in pages:
            summary = page.summary_line.strip().lower()
            if summary:
                duplicate_summaries.setdefault(summary, []).append(page.name)
        duplicate_summaries = {summary: names for summary, names in duplicate_summaries.items() if len(names) > 1}
        return {
            "total_pages": len(pages),
            "orphans": sorted(orphans),
            "broken_links": broken_links,
            "pages_without_summary": sorted(pages_without_summary),
            "duplicate_summaries": duplicate_summaries,
        }

    def rebuild_index(self) -> None:
        pages = self.list_pages()
        groups: Dict[str, List[WikiPage]] = {}
        for page in pages:
            group = page.name.split("/", 1)[0] if "/" in page.name else "root"
            groups.setdefault(group, []).append(page)
        lines = ["# Wiki Index", "", f"*{len(pages)} pages — updated {_iso_now()}*", ""]
        for group, group_pages in sorted(groups.items()):
            lines.append(f"## {group.title()}")
            lines.append("")
            for page in sorted(group_pages, key=lambda item: item.name):
                summary = page.summary_line or "(no summary)"
                lines.append(f"- [[{page.name}]] — {summary}")
            lines.append("")
        write_text_atomically(self.root / "index.md", "\n".join(lines) + "\n", encoding="utf-8")

    def stats(self) -> Dict[str, Any]:
        pages = self.list_pages()
        log_path = self.root / "log.md"
        log_entries = log_path.read_text(encoding="utf-8").count("\n## [") if log_path.exists() else 0
        return {
            "wiki_dir": str(self.root),
            "total_pages": len(pages),
            "index_exists": (self.root / "index.md").exists(),
            "log_exists": (self.root / "log.md").exists(),
            "log_entries": log_entries,
        }

    def _page_path(self, name: str) -> Path:
        safe_name = self._safe_page_name(name)
        path = (self.root / safe_name).with_suffix(".md").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _read_page_object(self, path: Path) -> WikiPage:
        return WikiPage(
            path=path,
            name=str(path.relative_to(self.root).with_suffix("")).replace("\\", "/"),
            content=path.read_text(encoding="utf-8"),
            mtime=path.stat().st_mtime,
        )

    def _append_log_locked(self, operation: str, description: str) -> None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        append_text_atomically(self.root / "log.md", f"\n## [{date}] {operation} | {description}\n", encoding="utf-8")

    def _safe_page_name(self, name: str) -> str:
        raw = str(name or "").strip().replace("\\", "/").strip("/")
        if not raw:
            raise ValueError("wiki page name required")
        parts = []
        for part in raw.split("/"):
            cleaned = re.sub(r"[^A-Za-z0-9_. -]", "_", part).strip(". ")
            if cleaned:
                parts.append(cleaned)
        if not parts:
            raise ValueError("wiki page name required")
        return "/".join(parts)

    def _within_root(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_frontmatter(content: str, *, created: str, updated: str, source: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    body = _FRONTMATTER_RE.sub("", content or "").strip()
    metadata = dict(metadata or {})
    frontmatter = "\n".join([
        "---",
        f"created: {created}",
        f"updated: {updated}",
        f"sources: [{source}]",
        f"data_classification: {metadata.get('data_classification', 'public')}",
        f"data_protection_modified: {str(bool(metadata.get('data_protection_modified', False))).lower()}",
        "---",
        "",
    ])
    return frontmatter + body + "\n"


def _extract_json_object(text: str) -> Dict[str, Any]:
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_DEFAULT_SCHEMA = """# Wiki Schema

Use focused markdown pages for durable Blackboard project knowledge.

## Page guidance

- One concept, decision, template, or project fact per page.
- Prefer concrete evidence over speculation.
- Link related pages with `[[Page Name]]` syntax.
- Do not store secrets or API key values.
"""
