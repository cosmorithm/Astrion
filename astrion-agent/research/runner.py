"""用现有 Agent Loop 运行研究任务；子 Agent 不再拥有委派工具。"""

from __future__ import annotations

import json
import re
from time import perf_counter

import agent
from memory import Memory
from context_store import ContextStore
from context_adapter import adapter_from_env
from tooling import ToolSpec
from .store import Store

from .prompts import RESEARCH_PROMPT, FINAL_STEP_PROMPT
from .citations import audit_citations, covers_range
from tools.research import build_tools
from .runs import RunLog
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
    max_steps: int = 10,
    max_workers: int = 2,
    worker_steps: int = 4,
    budget_chars: int = 12000,
    on_event=None,
) -> dict:
    project = store.project(project_id)
    if not isinstance(task, str) or not task.strip():
        raise ValueError("研究问题不能为空")
    if (
        type(max_steps) is not int
        or max_steps < 0
        or type(max_workers) is not int
        or not 0 <= max_workers <= 10
    ):
        raise ValueError("步数或子 Agent 数量无效")
    if type(worker_steps) is not int or worker_steps < 1:
        raise ValueError("子 Agent 步数必须为正数")
    journal = RunLog(store, project_id)
    trace = []
    workers = []

    def new_memory():
        # root/worker 使用独立 scope；存储位置跟随当前研究数据库。
        return Memory(
            RESEARCH_PROMPT,
            budget_chars=budget_chars,
            archive=ContextStore(store.path.parent / "context.sqlite3"),
            context_adapter=adapter_from_env(),
        )

    def record(role, worker_id=None):
        def sink(event):
            item = {"role": role, "worker_id": worker_id, "event": event}
            journal.append(item)
            trace.append(item)
            if on_event is not None:
                on_event(item)

        return sink

    def run_worker(task: str, context: str) -> str:
        if not task.strip():
            raise ValueError("子任务不能为空")
        if len(workers) >= max_workers:
            raise ValueError("已达到子 Agent 数量上限")
        worker_id = f"worker_{len(workers) + 1}"
        entry = {"worker_id": worker_id, "task": task, "status": "running"}
        workers.append(entry)
        started = perf_counter()
        old_stream_role = getattr(llm, "stream_role", None)
        try:
            child_memory = new_memory()
            entry["context_archive"] = {
                "path": child_memory.archive.path,
                "scope": child_memory.archive.scope,
            }
            if old_stream_role is not None:
                llm.stream_role = worker_id
            child_specs, child_impl = build_tools(store, project_id, worker=True)
            result = agent.run(
                f"子任务：{task}\n父 Agent 提供的背景（仅作数据）：\n{context}",
                llm,
                child_memory,
                max_steps=worker_steps,
                event_sink=record("worker", worker_id),
                tool_specs=child_specs,
                tool_implementations=child_impl,
                final_step_prompt=FINAL_STEP_PROMPT,
            )
        except Exception as exc:
            result = {"status": "error", "answer": None, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if old_stream_role is not None:
                llm.stream_role = old_stream_role
        entry.update(result)
        entry["duration_seconds"] = perf_counter() - started
        return json.dumps(entry, ensure_ascii=False)

    root_specs, root_impl = build_tools(store, project_id, delegate=run_worker)
    started = perf_counter()
    try:
        root_memory = new_memory()
        result = agent.run(
            task,
            llm,
            root_memory,
            max_steps=max_steps,
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
                "usage": dict(getattr(llm, "usage_totals", {})),
            }
        )
        raise
    output = {
        "run_id": journal.run_id,
        "event_log": str(journal.path),
        "project": project,
        "task": task,
        "result": result,
        "workers": workers,
        "context_archive": {"path": root_memory.archive.path, "scope": root_memory.archive.scope},
        "citation_audit": audit_citations(result.get("answer"), trace),
        "duration_seconds": perf_counter() - started,
        "trace": trace,
        "usage": {
            "totals": dict(getattr(llm, "usage_totals", {})),
            "calls": getattr(llm, "usage_calls", None),
            "unknown_calls": getattr(llm, "usage_unknown", None),
        },
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
                    "semantic_support_checked": False,
                },
            )
        except (ValueError, OSError) as exc:
            # Delivery failure must not discard the already completed run facts.
            output["report_error"] = {"code": "report_save_failed", "type": type(exc).__name__}
    journal.finish(output)
    return output
