#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看已经生成的 BFCL 运行成绩；不调用模型。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_RUNS_DIR = Path(__file__).resolve().parent / "benchmark_runs" / "bfcl"
REPORT_NAME = "bfcl_run_report.json"


class ScoreReportError(ValueError):
    """BFCL 成绩报告不存在或格式无效。"""


def load_report(path: Path | str) -> dict:
    """读取一份 BFCL 统一报告。"""
    target = Path(path).expanduser().resolve()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScoreReportError(f"无法读取报告 {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScoreReportError(f"报告不是有效 JSON {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScoreReportError(f"报告顶层必须是 JSON 对象: {target}")
    if data.get("kind") != "bfcl_official_run":
        raise ScoreReportError(f"不是 BFCL 运行报告: {target}")
    return data


def resolve_report(selector: str, runs_dir: Path | str = DEFAULT_RUNS_DIR) -> Path:
    """把运行 ID、运行目录或报告文件解析成报告路径。"""
    direct = Path(selector).expanduser()
    if direct.exists():
        candidate = direct / REPORT_NAME if direct.is_dir() else direct
    else:
        candidate = Path(runs_dir).expanduser() / selector / REPORT_NAME
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ScoreReportError(f"没有找到 BFCL 报告: {candidate}")
    return candidate


def summarize_report(report: dict, report_path: Path | str) -> dict:
    """提取适合人读和脚本消费的稳定成绩字段。"""
    path = Path(report_path).expanduser().resolve()
    benchmark = report.get("benchmark") if isinstance(report.get("benchmark"), dict) else {}
    outputs = (
        report.get("official_outputs")
        if isinstance(report.get("official_outputs"), dict)
        else {}
    )
    commands = report.get("commands") if isinstance(report.get("commands"), dict) else {}
    generate = commands.get("generate") if isinstance(commands.get("generate"), dict) else {}
    evaluate = commands.get("evaluate") if isinstance(commands.get("evaluate"), dict) else {}
    generate_seconds = generate.get("duration_seconds")
    evaluate_seconds = evaluate.get("duration_seconds")
    durations = [
        value for value in (generate_seconds, evaluate_seconds)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]

    category_scores = outputs.get("official_category_scores")
    if not isinstance(category_scores, list):
        category_scores = []
    token_usage = outputs.get("token_usage")
    if not isinstance(token_usage, dict):
        token_usage = None

    return {
        "run_id": path.parent.name,
        "status": report.get("status"),
        "created_at": report.get("created_at"),
        "model": report.get("model"),
        "profile": benchmark.get("profile"),
        "case_count": benchmark.get("case_count"),
        "expected_scored_cases": benchmark.get("expected_scored_cases"),
        "score": outputs.get("derived_subset_micro_accuracy"),
        "correct_cases": outputs.get("correct_cases"),
        "scored_cases": outputs.get("scored_cases"),
        "category_scores": category_scores,
        "token_usage": token_usage,
        "model_latency_seconds": outputs.get("model_latency_seconds"),
        "generate_seconds": generate_seconds,
        "evaluate_seconds": evaluate_seconds,
        "command_total_seconds": sum(durations) if durations else None,
        "error": report.get("error"),
        "report_path": str(path),
    }


def discover_runs(runs_dir: Path | str = DEFAULT_RUNS_DIR) -> list[dict]:
    """按报告修改时间从新到旧列出运行；损坏报告也保留为 invalid。"""
    root = Path(runs_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    discovered = []
    for path in root.glob(f"*/{REPORT_NAME}"):
        try:
            summary = summarize_report(load_report(path), path)
        except ScoreReportError as exc:
            summary = {
                "run_id": path.parent.name,
                "status": "invalid",
                "score": None,
                "correct_cases": None,
                "scored_cases": None,
                "model": None,
                "report_path": str(path.resolve()),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        summary["modified_at"] = path.stat().st_mtime
        discovered.append(summary)
    return sorted(discovered, key=lambda item: item["modified_at"], reverse=True)


def latest_completed_report(runs_dir: Path | str = DEFAULT_RUNS_DIR) -> Path:
    """找到最近一次 status=completed 的报告。"""
    for summary in discover_runs(runs_dir):
        if summary.get("status") == "completed":
            return Path(summary["report_path"])
    raise ScoreReportError("没有找到已完成的 BFCL 运行；可先用 --list 查看历史记录")


def _percent(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2%}"
    return "暂无"


def format_summary(summary: dict) -> str:
    """格式化单次运行成绩。"""
    correct = summary.get("correct_cases")
    scored = summary.get("scored_cases")
    count_text = f"{correct}/{scored}" if correct is not None and scored is not None else "暂无"
    expected_scored = summary.get("expected_scored_cases")
    if expected_scored is not None and expected_scored != summary.get("case_count"):
        range_text = (
            f"{summary.get('profile')}（生成条目 {summary.get('case_count')}，"
            f"预计计分 {expected_scored}）"
        )
    else:
        range_text = f"{summary.get('profile')}（配置 {summary.get('case_count')} 条）"
    lines = [
        "BFCL 成绩",
        f"运行 ID：{summary.get('run_id')}",
        f"状态：{summary.get('status')}",
        f"模型：{summary.get('model')}",
        f"范围：{range_text}",
        f"总分：{_percent(summary.get('score'))}（{count_text}）",
    ]

    categories = summary.get("category_scores") or []
    if categories:
        lines.append("分类成绩：")
        for item in categories:
            lines.append(
                f"  - {item.get('category')}: {_percent(item.get('accuracy'))} "
                f"({item.get('correct_count')}/{item.get('total_count')})"
            )

    usage = summary.get("token_usage")
    if usage:
        lines.append(
            "Token："
            f"input {usage.get('input_tokens')}，"
            f"output {usage.get('output_tokens')}，"
            f"total {usage.get('total_tokens')}"
        )
    model_latency = summary.get("model_latency_seconds")
    if isinstance(model_latency, (int, float)) and not isinstance(model_latency, bool):
        lines.append(f"模型延迟：{summary['model_latency_seconds']:.2f} 秒")
    if summary.get("command_total_seconds") is not None:
        duration_parts = []
        for name, value in (
            ("generate", summary.get("generate_seconds")),
            ("evaluate", summary.get("evaluate_seconds")),
        ):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                duration_parts.append(f"{name} {value:.2f} 秒")
        duration_parts.append(f"合计 {summary['command_total_seconds']:.2f} 秒")
        lines.append("命令耗时：" + "，".join(duration_parts))
    if summary.get("error"):
        error = summary["error"]
        if isinstance(error, dict):
            lines.append(f"错误：{error.get('type')}: {error.get('message')}")
        else:
            lines.append(f"错误：{error}")
    lines.append(f"报告：{summary.get('report_path')}")
    lines.append("说明：这是当前 subset 成绩，不是 BFCL 完整排行榜总分。")
    return "\n".join(lines)


def format_run_list(runs: list[dict]) -> str:
    if not runs:
        return "没有找到 BFCL 运行报告。"
    lines = ["BFCL 历史运行（新 → 旧）"]
    for item in runs:
        correct = item.get("correct_cases")
        scored = item.get("scored_cases")
        count_text = f"{correct}/{scored}" if correct is not None and scored is not None else "-"
        lines.append(
            f"- {item.get('run_id')} | {item.get('status')} | "
            f"{_percent(item.get('score'))} | {count_text} | {item.get('model') or '-'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看 BFCL 运行成绩；不会调用模型")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR,
                        help="BFCL 运行目录；默认 benchmark_runs/bfcl")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="列出全部历史运行")
    group.add_argument("--run", default="", metavar="ID_OR_PATH",
                       help="指定运行 ID、运行目录或 bfcl_run_report.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.list:
        runs = discover_runs(args.runs_dir)
        print(json.dumps(runs, ensure_ascii=False, indent=2) if args.json else format_run_list(runs))
        return 0

    try:
        report_path = (
            resolve_report(args.run, args.runs_dir)
            if args.run
            else latest_completed_report(args.runs_dir)
        )
        summary = summarize_report(load_report(report_path), report_path)
    except ScoreReportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
