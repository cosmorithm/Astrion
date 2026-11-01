"""用现有 Agent Loop 运行研究任务；子 Agent 不再拥有委派工具。"""

from __future__ import annotations

from threading import RLock
from time import perf_counter

import agent
from memory import Memory
from context_store import ContextStore
from context_adapter import adapter_from_env
from .store import Store

from .prompts import FINAL_STEP_PROMPT, run_prompt
from .citations import audit_citations, covers_range, observed_ranges
from tools.research import build_tools
from .runs import RunLog
from .config import DEFAULT_STEPS, DEFAULT_WORKERS, validate_budgets
from .continuation import fork_parent
from .workers import WorkerPool
from .storage.artifacts import Artifacts
from response_contract import answer_error

# Backward-compatible names; declarations have one authority in tools/knowledge.py.
from tools.knowledge import SEARCH_DOCS, READ_DOC, SEARCH_MEM


def run_research(
    store: Store,
    project_id: int,
    task: str,
    llm,
    *,
    max_steps: int = DEFAULT_STEPS,
    max_workers: int = DEFAULT_WORKERS,
    worker_steps: int = DEFAULT_STEPS,
    budget_chars: int = 12000,
    on_event=None,
    continue_run_id: str | None = None,
) -> dict:
    project = store.project(project_id)
    if not isinstance(task, str) or not task.strip():
        raise ValueError("研究问题不能为空")
    validate_budgets(max_steps, max_workers, worker_steps)
    prompt = run_prompt(max_steps, worker_steps, max_workers)
    parent, root_memory, inherited_ranges = None, None, []
    if continue_run_id:
        parent, root_memory, inherited_ranges = fork_parent(
            store, project_id, continue_run_id, prompt=prompt, budget_chars=budget_chars)
    journal = RunLog(store, project_id)
    trace = []
    event_lock = RLock()

    def new_memory():
        # root/worker 使用独立 scope；存储位置跟随当前研究数据库。
        return Memory(
            prompt,
            budget_chars=budget_chars,
            archive=ContextStore(store.path.parent / "context.sqlite3"),
            context_adapter=adapter_from_env(),
        )

    def record(role, worker_id=None):
        def sink(event):
            with event_lock:
                item = {"role": role, "worker_id": worker_id, "event": event}
                journal.append(item)
                trace.append(item)
                if on_event is not None:
                    on_event(item)

        return sink

    pool = WorkerPool(store, project_id, llm, limit=max_workers, steps=worker_steps,
                      new_memory=new_memory, record=record, run_id=journal.run_id)
    root_specs, root_impl = build_tools(store, project_id, delegate=pool.run_one, batch_delegate=pool.run_many)
    started = perf_counter()
    try:
        if root_memory is None:
            root_memory = new_memory()
        result = agent.run(
            root_memory.runtime.get("task", parent["task"]) if parent else task,
            llm,
            root_memory,
            max_steps=max_steps,
            resume=bool(parent), user_reply=task if parent else None,
            task_plan=root_memory.runtime.get("task_plan") if parent else None,
            event_sink=record("root"),
            tool_specs=root_specs,
            tool_implementations=root_impl,
            final_step_prompt=FINAL_STEP_PROMPT,
        )
    except Exception as exc:
        journal.finish(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "project_id": project_id,
                "duration_seconds": perf_counter() - started,
                "usage": pool.usage(),
                "run_id": journal.run_id, "project": project, "task": task,
                "parent_run_id": continue_run_id,
            }
        )
        raise
    output = {
        "run_id": journal.run_id,
        "event_log": str(journal.path),
        "project": project,
        "task": task,
        "result": result,
        "workers": pool.entries,
        "parent_run_id": continue_run_id,
        "budgets": {"max_steps": max_steps, "max_workers": max_workers, "worker_steps": worker_steps},
        "observed_ranges": sorted(set(map(tuple, inherited_ranges)) | set(observed_ranges(trace))),
        "context_archive": {"path": root_memory.archive.path, "scope": root_memory.archive.scope},
        "citation_audit": audit_citations(result.get("answer"), trace, inherited_ranges),
        "duration_seconds": perf_counter() - started,
        "trace": trace,
        "usage": pool.usage(),
    }

    invalid = answer_error(result.get("answer"))
    if invalid:
        result.update(status="error", answer=None, error=invalid)
        output["citation_audit"] = audit_citations(None, trace)
    if result.get("answer"):
        try:
            output["report_artifact"] = Artifacts(store).put(
                project_id,
                "research-report.md",
                result["answer"].encode("utf-8"),
                provenance={
                    "kind": "agent_report_unverified",
                    "run_id": journal.run_id,
                    "task_status": result.get("status"),
                    "parent_run_id": continue_run_id,
                    "semantic_support_checked": False,
                },
            )
        except (ValueError, OSError) as exc:
            # Delivery failure must not discard the already completed run facts.
            output["report_error"] = {"code": "report_save_failed", "type": type(exc).__name__}
    journal.finish(output)
    return output
