"""命令行入口：python3 -m evaluation --case sum_001。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import agent

from .runner import evaluate_case


def main() -> None:
    parser = argparse.ArgumentParser(description="运行任务结束后的 agent 评测")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"), help="评测任务 JSON 文件")
    parser.add_argument("--case", default="", help="只运行指定任务 id；默认运行全部任务")
    parser.add_argument("--list", action="store_true", help="列出任务，不调用模型或生成评测报告")
    parser.add_argument("--steps", type=int, default=10, help="agent 的最大决策轮数")
    parser.add_argument("--budget", type=int, default=12000, help="agent 记忆预算（字符）")
    parser.add_argument("--model", default="", help="临时覆盖 AGENT_MODEL")
    parser.add_argument("--base-url", default="", help="临时覆盖 AGENT_BASE_URL")
    parser.add_argument("--report-dir", type=Path, default=None, help="报告目录；默认 toy-agent/eval_reports")
    args = parser.parse_args()

    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
            raise ValueError("任务文件必须是对象列表")
        ids = [case.get("id") for case in cases]
        if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
            raise ValueError("每个任务都需要非空 id")
        if len(set(ids)) != len(ids):
            raise ValueError("任务 id 不能重复")
        if args.case:
            cases = [case for case in cases if case["id"] == args.case]
            if not cases:
                raise ValueError(f"没有找到评测任务: {args.case}")
        if not cases:
            raise ValueError("评测任务列表为空")
        if args.steps < 0 or args.budget <= 0:
            raise ValueError("--steps 必须非负，--budget 必须为正")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.list:
        print(json.dumps({"cases": [{"id": case["id"], "description": case.get("description", "")}
                                    for case in cases]}, ensure_ascii=False, indent=2))
        return

    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(os.environ.get("AGENT_API_KEY"),
                               os.environ.get("DEEPSEEK_API_KEY"),
                               os.environ.get("OPENAI_API_KEY"))
    if not key:
        parser.error("没有可用的 API key，请按 toy-agent/README.md 配置")
    base_url = args.base_url or os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com"
    model = args.model or os.environ.get("AGENT_MODEL") or "deepseek-chat"

    def create_llm() -> agent.ChatLLM:
        return agent.ChatLLM(key=key, base_url=base_url, model=model)

    # agent 的运行日志放在 stderr；stdout 保持为可解析的 JSON 报告。
    reports = []
    try:
        with redirect_stdout(sys.stderr):
            for case in cases:
                report = evaluate_case(case, create_llm, max_steps=args.steps, budget_chars=args.budget,
                                       report_dir=args.report_dir)
                reports.append(report)
                if report["report_path"] is not None:
                    print(f"报告已保存（{report['case_id']}）: {report['report_path']}")
                else:
                    error = report["report_error"]
                    print(f"报告保存失败（{report['case_id']}）: {error['type']}: {error['message']}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        output = json.dumps({"reports": reports}, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        print(f"评测结果无法输出为 JSON: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(output)
    failures = [report for report in reports if report["report_error"] is not None]
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
