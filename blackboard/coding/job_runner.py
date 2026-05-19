from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from blackboard.coding.budget_allocator import ContextBudgetAllocator
from blackboard.coding.jobs import BackgroundJobManager
from blackboard.coding.reviewer import CodeReviewer
from blackboard.coding.worker import init_coding_worker
from blackboard.kernel.config import load_config
from blackboard.kernel.prompts import init_prompts
from blackboard.providers.registry import init_provider_registry
from blackboard.workspace.coding_settings import load_coding_overrides, merge_coding_config
from blackboard.workspace.key_overrides import load_keys
from blackboard.workspace.model_overrides import load_model_overrides, merge_into_profiles
from blackboard.workspace.role_overrides import load_overrides, merge_into_config
from blackboard.wiki.manager import WikiManager


_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _ROOT / "config.yaml"


def _build_registry(data_root: Path):
    cfg = load_config(_CONFIG_PATH)
    init_prompts(_ROOT / "data" / "prompts.yaml")
    providers_section = cfg.section("providers")
    role_overrides = load_overrides(data_root)
    if role_overrides:
        providers_section = {
            **providers_section,
            "roles": merge_into_config(providers_section.get("roles") or {}, role_overrides),
        }
    key_overrides = load_keys(data_root)
    if key_overrides:
        merged_profiles = {pid: dict(prof) for pid, prof in (providers_section.get("profiles") or {}).items()}
        for pid, value in key_overrides.items():
            if pid in merged_profiles:
                merged_profiles[pid]["api_key"] = value
        providers_section = {**providers_section, "profiles": merged_profiles}
    model_overrides = load_model_overrides(data_root)
    if model_overrides:
        providers_section = {
            **providers_section,
            "profiles": merge_into_profiles(providers_section.get("profiles") or {}, model_overrides),
        }
    return cfg, init_provider_registry(providers_section)


async def _run(job_id: str, db_path: Path, data_root: Path, worktree_dir: str, base_branch: str) -> int:
    cfg, registry = _build_registry(data_root)
    coding_cfg = merge_coding_config(cfg.section("coding"), load_coding_overrides(data_root))
    wiki_manager = WikiManager(data_root / "wiki")
    worker = init_coding_worker(
        registry,
        data_dir=data_root,
        max_iterations=int(cfg.get("react.coding_max_iterations", 12)),
        max_retries=int(cfg.get("coding.max_job_retries", 2)),
        wiki_manager=wiki_manager,
    )
    worker._allocator = ContextBudgetAllocator(
        total_budget=int(cfg.get("context.budget", 200_000)),
        allocations=cfg.section("context.budget_allocations") or None,
    )
    reviewer = CodeReviewer(registry=registry)
    manager = BackgroundJobManager(
        db_path=db_path,
        worker=worker,
        reviewer=reviewer,
        bus=None,
        worktree_dir=worktree_dir,
        base_branch=base_branch,
        max_concurrent=int(coding_cfg.get("max_concurrent", cfg.get("coding.max_concurrent", 4))),
    )
    try:
        await manager.start(recover_running=False, start_monitor=False)
        await manager._run_job(job_id)
        return 0
    finally:
        await manager.stop()
        await registry.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--worktree-dir", default=".worktrees")
    parser.add_argument("--base-branch", default="main")
    args = parser.parse_args()
    return asyncio.run(
        _run(
            job_id=str(args.job_id),
            db_path=Path(args.db_path).resolve(),
            data_root=Path(args.data_root).resolve(),
            worktree_dir=str(args.worktree_dir),
            base_branch=str(args.base_branch),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
