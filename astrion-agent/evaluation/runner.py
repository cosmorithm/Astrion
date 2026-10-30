"""运行一个独立评测任务，保存事实，再调用赛后裁判。"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Callable

import agent
from events import EVENT_SCHEMA_VERSION, EventRecorder
from memory import Memory

from .judges import JudgeRegistry, default_registry
from .reporting import save_report


def _validate_case(case: dict) -> None:
    if not isinstance(case, dict):
        raise ValueError("评测任务必须是对象")
    if not isinstance(case.get("id"), str) or not case["id"].strip():
        raise ValueError("评测任务需要非空 id")
    if not isinstance(case.get("task"), str) or not case["task"].strip():
        raise ValueError("评测任务需要非空 task")
    files = case.get("initial_files", {})
    if not isinstance(files, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in files.items()):
        raise ValueError("initial_files 必须是文件路径到文本的映射")
    if not isinstance(case.get("judges", []), list):
        raise ValueError("judges 必须是列表")


def _prepare_files(root: Path, files: dict) -> None:
    for name, content in files.items():
        target = agent.safe_path(name, sandbox=root)
        if target == root:
            raise ValueError("初始文件路径不能是沙箱目录")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _snapshot(root: Path) -> tuple[dict, dict]:
    files, errors = {}, {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        try:
            files[name] = agent.safe_path(name, sandbox=root).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return files, errors


def _usage_snapshot(llm) -> dict | None:
    totals = getattr(llm, "usage_totals", None)
    calls = getattr(llm, "usage_calls", None)
    unknown = getattr(llm, "usage_unknown", None)
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(totals, dict) or any(type(totals.get(k)) is not int for k in fields):
        return None
    if type(calls) is not int or type(unknown) is not int:
        return None
    return {"totals": {k: totals[k] for k in fields}, "calls": calls, "unknown": unknown}


def _usage_difference(before: dict | None, after: dict | None, interrupted: bool) -> dict | None:
    if before is None or after is None:
        return None
    totals = {k: after["totals"][k] - before["totals"][k] for k in before["totals"]}
    calls = after["calls"] - before["calls"]
    unknown = after["unknown"] - before["unknown"]
    if any(value < 0 for value in totals.values()) or calls < 0 or unknown < 0 or unknown > calls:
        return None
    return {
        "known_totals": totals,
        "reported_calls": calls - unknown,
        "unknown_calls": unknown,
        "complete": unknown == 0 and not interrupted,
    }


def _setup_error(exc: BaseException, max_steps: int, budget_chars: int) -> dict:
    """准备失败时没有 agent 耗时，也不把结果当作任务失败。"""
    return {
        "result": {"status": "setup_error", "answer": None, "steps": 0,
                   "error": {"type": type(exc).__name__, "message": str(exc)}},
        "duration_seconds": None,
        "model": None,
        "agent_config": {"max_steps": max_steps, "budget_chars": budget_chars,
                         "tools": copy.deepcopy(agent.TOOLS)},
        "token_usage": None,
        "run_id": None,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "trace": [],
        "artifacts": {},
        "artifact_errors": {},
    }


def _finish_report(case: dict, run_record: dict, registry: JudgeRegistry,
                   report_dir: Path | str | None) -> dict:
    if run_record["result"]["status"] == "setup_error":
        judgments = [
            {"judge": spec.get("name") if isinstance(spec, dict) else None,
             "status": "unknown", "reason": "评测环境准备失败，裁判未运行", "duration_seconds": None}
            for spec in case.get("judges", [])
        ]
    else:
        judgments = registry.evaluate(copy.deepcopy(case), run_record)
    report = {"case_id": case["id"], "case": copy.deepcopy(case), "run": run_record,
              "judgments": judgments, "report_path": None, "report_error": None}
    try:
        report["report_path"] = str(save_report(report, report_dir))
    except (OSError, TypeError, ValueError) as exc:
        # 保留运行结果供调用方查看；不能假装报告已经保存成功。
        report["report_error"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def evaluate_case(
    case: dict,
    llm_factory: Callable[[], object],
    registry: JudgeRegistry | None = None,
    *,
    max_steps: int = 10,
    budget_chars: int = 12000,
    report_dir: Path | str | None = None,
) -> dict:
    """准备独立目录，运行、赛后评判并自动保存一份报告。"""
    _validate_case(case)
    if type(max_steps) is not int or max_steps < 0:
        raise ValueError("max_steps 必须是非负整数")
    if type(budget_chars) is not int or budget_chars <= 0:
        raise ValueError("budget_chars 必须是正整数")
    registry = registry if registry is not None else default_registry()

    try:
        temporary = tempfile.TemporaryDirectory(prefix="astrion-agent-eval-")
    except (Exception, SystemExit) as exc:
        return _finish_report(case, _setup_error(exc, max_steps, budget_chars), registry, report_dir)

    with temporary as directory:
        root = Path(directory)
        try:
            _prepare_files(root, case.get("initial_files", {}))
            llm = llm_factory()
            memory = Memory(agent.SYSTEM_PROMPT, budget_chars=budget_chars)
        except (Exception, SystemExit) as exc:
            run_record = _setup_error(exc, max_steps, budget_chars)
        else:
            recorder = EventRecorder()
            usage_before = _usage_snapshot(llm)

            # 只计 agent.run：准备目录、读取快照、裁判和保存报告均不计入。
            started = perf_counter()
            try:
                result = agent.run(case["task"], llm, memory, max_steps=max_steps,
                                   sandbox=root, event_sink=recorder)
            except (Exception, SystemExit) as exc:
                trace = recorder.events
                result = {
                    "status": "error", "answer": None,
                    "steps": sum(
                        event["type"] == "model.call.finished"
                        and event["payload"].get("purpose") == "decision"
                        for event in trace
                    ),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            finally:
                duration = perf_counter() - started

            usage_after = _usage_snapshot(llm)
            artifacts, artifact_errors = _snapshot(root)
            trace = recorder.events
            run_record = {
                "result": result,
                "duration_seconds": duration,
                "model": getattr(llm, "model", None),
                "agent_config": {
                    "max_steps": max_steps,
                    "budget_chars": budget_chars,
                    "tools": copy.deepcopy(agent.TOOLS),
                },
                "token_usage": _usage_difference(usage_before, usage_after, result["status"] == "error"),
                "run_id": recorder.run_id,
                "event_schema_version": EVENT_SCHEMA_VERSION,
                "trace": trace,
                "artifacts": artifacts,
                "artifact_errors": artifact_errors,
            }

    # 临时目录销毁后再评判和保存；准备失败时裁判不会被调用。
    return _finish_report(case, run_record, registry, report_dir)
