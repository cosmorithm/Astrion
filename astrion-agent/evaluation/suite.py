"""汇总多次任务的裁判结果，并保存独立的总报告。"""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

from .reporting import save_report


def _outcome(judgments: list[dict]) -> str:
    """有明确失败即失败；全部通过才通过；其余情况无法判定。"""
    statuses = [item.get("status") for item in judgments]
    if "fail" in statuses:
        return "fail"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    return "unresolved"


def summarize_suite(reports: list[dict], report_dir: Path | str | None = None) -> dict:
    """根据各任务已有的裁判结论统计通过率，不重新运行 agent 或裁判。"""
    cases = []
    for report in reports:
        run = report.get("run") or {}
        result = run.get("result") or {}
        judgments = report.get("judgments") or []
        cases.append({
            "case_id": report.get("case_id"),
            "outcome": _outcome(judgments),
            "run_status": result.get("status", "unknown"),
            "duration_seconds": run.get("duration_seconds"),
            "judgments": copy.deepcopy(judgments),
            "report_path": report.get("report_path"),
            "report_error": copy.deepcopy(report.get("report_error")),
        })

    counts = Counter(case["outcome"] for case in cases)
    total = len(cases)
    passed, failed, unresolved = counts["pass"], counts["fail"], counts["unresolved"]
    scored = passed + failed
    run_status_counts = Counter(case["run_status"] for case in cases)
    summary = {
        "kind": "evaluation_summary",
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": failed,
        "unresolved_cases": unresolved,
        "pass_rate_percent": round(100 * passed / total, 2) if total else None,
        "scored_pass_rate_percent": round(100 * passed / scored, 2) if scored else None,
        "run_status_counts": dict(run_status_counts),
        "run_error_cases": run_status_counts["error"] + run_status_counts["setup_error"],
        "saved_case_reports": sum(case["report_path"] is not None for case in cases),
        "cases": cases,
        "report_path": None,
        "report_error": None,
    }
    try:
        summary["report_path"] = str(save_report(summary, report_dir, filename_prefix="suite"))
    except (OSError, TypeError, ValueError) as exc:
        summary["report_error"] = {"type": type(exc).__name__, "message": str(exc)}
    return summary
