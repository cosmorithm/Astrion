"""python -m evaluation.context --case C01 --case C02 --case C03"""

from __future__ import annotations
import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import sys

import agent
from .runner import MODES, load_cases, new_report, run_group, save_report, summarize


def main():
    parser = argparse.ArgumentParser(
        description="固定历史的上下文策略对照（真实模型会产生费用）"
    )
    parser.add_argument(
        "--cases", type=Path, default=Path(__file__).with_name("cases.json")
    )
    parser.add_argument(
        "--case", action="append", default=[], help="选择样例，可重复；默认全部"
    )
    parser.add_argument("--mode", choices=(*MODES, "all"), default="all")
    parser.add_argument(
        "--output", type=Path, help="结果 JSON；默认 eval_reports/context_<id>.json"
    )
    parser.add_argument(
        "--budget-factor",
        type=float,
        default=1.25,
        help="输入预算为初始消息字符的倍数；默认 1.25 对应 80% 水位",
    )
    parser.add_argument("--model", default="", help="临时覆盖模型名")
    parser.add_argument("--base-url", default="", help="临时覆盖模型服务地址")
    parser.add_argument("--list", action="store_true", help="仅列出样例，不调用模型")
    parser.add_argument(
        "--followups",
        type=int,
        default=0,
        choices=range(0, 3),
        help="在同一视图下追加 0–2 条固定追问",
    )
    args = parser.parse_args()
    try:
        data = load_cases(args.cases)
        available = {case["id"]: case for case in data["cases"]}
        if len(set(args.case)) != len(args.case) or any(
            id not in available for id in args.case
        ):
            raise ValueError("存在重复或未知的 case ID")
        cases = [available[id] for id in args.case] if args.case else data["cases"]
        if not cases:
            raise ValueError("样例集合为空")
        if not 0.1 <= args.budget_factor <= 2.0:
            raise ValueError("budget-factor 必须在 0.1–2.0")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.list:
        for case in cases:
            print(f'{case["id"]}\t{case["kind"]}\t{case["question"]}')
        return
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(
        os.environ.get("AGENT_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    if not key:
        parser.error("模型密钥未配置")
    base = (
        args.base_url or os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com"
    )
    model = args.model or os.environ.get("AGENT_MODEL") or "deepseek-chat"
    modes = MODES if args.mode == "all" else (args.mode,)
    report = new_report(
        args.cases, cases, model, modes, budget_factor=args.budget_factor
    )
    followup_data = None
    if args.followups:
        import hashlib, json

        followup_path = Path(__file__).with_name("followups.json")
        raw_followups = followup_path.read_bytes()
        followup_data = json.loads(raw_followups)["cases"]
        report["conditions"]["followups"] = args.followups
        report["conditions"]["followups_sha256"] = hashlib.sha256(
            raw_followups
        ).hexdigest()
    parsed = urlsplit(base)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    report["provider_base_url"] = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    path = args.output or Path("eval_reports") / (report["run_id"] + ".json")
    if path.exists():
        parser.error("输出文件已存在，拒绝覆盖")
    save_report(report, path)
    for case in cases:
        for mode in modes:
            print(f'开始 {case["id"]} / {mode}', file=sys.stderr, flush=True)
            llm = agent.ChatLLM(key, base, model)
            group = run_group(
                case,
                mode,
                llm,
                budget_factor=args.budget_factor,
                followups=(
                    followup_data[case["id"]][: args.followups] if followup_data else ()
                ),
            )
            report["groups"].append(group)
            report["summary"] = summarize(report["groups"])
            save_report(report, path)
            print(
                f'完成 {case["id"]} / {mode}: 构建={group["construction"]["status"]} '
                f'答案={group["score"]["correct"] if group["score"] else "未知"} '
                f'已知 token={group["usage"]["known_totals"]["total_tokens"]}',
                file=sys.stderr,
                flush=True,
            )
    print(f"报告: {path.resolve()}")
    for mode, row in report["summary"].items():
        if row["cases"]:
            target = (
                "不适用"
                if mode == "append"
                else f'{row["applied_and_within_target"]}/{row["cases"]}'
            )
            print(
                f'{mode}: 正确 {row["answer_correct"]}/{row["answer_scored"]}，'
                f"构建成功且达标 {target}，"
                f'已知 token {row["known_tokens"]}，耗时 {row["duration_seconds"]:.1f}s'
            )


if __name__ == "__main__":
    main()
