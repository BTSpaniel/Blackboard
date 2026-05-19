from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set


@dataclass(frozen=True)
class ScopeViolation:
    violation_type: str
    severity: str
    file_path: str = ""
    symbol: str = ""
    description: str = ""


@dataclass(frozen=True)
class ScopeCheckResult:
    passed: bool
    violations: List[ScopeViolation] = field(default_factory=list)
    files_in_scope: int = 0
    files_out_of_scope: int = 0
    check_time_ms: float = 0.0


_DANGEROUS_IMPORTS: Set[str] = {
    "ctypes",
    "marshal",
    "pickle",
    "subprocess",
    "os.system",
    "shutil.rmtree",
    "pathlib.Path.unlink",
}


class ScopeGuard:
    def __init__(self, workspace_root: str = "") -> None:
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else Path(".").resolve()

    def check_file_scope(
        self,
        modified_files: List[str],
        declared_files: List[str],
        *,
        allow_new_files: bool = True,
    ) -> ScopeCheckResult:
        started = time.monotonic()
        declared = {self._normalize(path) for path in declared_files or [] if str(path or "").strip()}
        violations: List[ScopeViolation] = []
        in_scope = 0
        out_scope = 0
        if not declared:
            return ScopeCheckResult(passed=True, check_time_ms=(time.monotonic() - started) * 1000)
        for path in modified_files or []:
            normalized = self._normalize(path)
            if normalized in declared:
                in_scope += 1
                continue
            out_scope += 1
            exists = (self._workspace_root / normalized).exists()
            severity = "warning" if allow_new_files and not exists else "hard_fail"
            violations.append(ScopeViolation(
                violation_type="file_escape",
                severity=severity,
                file_path=normalized,
                description=f"File '{normalized}' changed outside declared task scope.",
            ))
        return ScopeCheckResult(
            passed=not any(violation.severity == "hard_fail" for violation in violations),
            violations=violations,
            files_in_scope=in_scope,
            files_out_of_scope=out_scope,
            check_time_ms=(time.monotonic() - started) * 1000,
        )

    def check_import_safety(self, file_path: str, before_source: str, after_source: str) -> ScopeCheckResult:
        started = time.monotonic()
        violations: List[ScopeViolation] = []
        before_imports = self._extract_imports(before_source)
        after_imports = self._extract_imports(after_source)
        added = after_imports - before_imports
        for import_name in sorted(added):
            if import_name in _DANGEROUS_IMPORTS or any(import_name.startswith(value + ".") for value in _DANGEROUS_IMPORTS):
                violations.append(ScopeViolation(
                    violation_type="import_escape",
                    severity="warning",
                    file_path=self._normalize(file_path),
                    symbol=import_name,
                    description=f"Potentially dangerous import '{import_name}' was added.",
                ))
        return ScopeCheckResult(
            passed=True,
            violations=violations,
            check_time_ms=(time.monotonic() - started) * 1000,
        )

    @staticmethod
    def _extract_imports(source: str) -> Set[str]:
        if not source:
            return set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        imports: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(str(alias.name or ""))
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = str(node.module or "")
                imports.add(module)
                for alias in node.names:
                    imports.add(f"{module}.{alias.name}")
        return {value for value in imports if value}

    @staticmethod
    def _normalize(path: str) -> str:
        return str(path or "").replace("\\", "/").strip().lstrip("./")
