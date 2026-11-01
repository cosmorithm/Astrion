"""Render an existing context evaluation JSON as a readable Markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import load_cases


def _escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render(report, cases):
    by_id = {case["id"]: case for case in cases}
    lines = [
        "# Astrion 上下文对照实验",
        "",
        f"- 运行：`{report['run_id']}`；模型：`{report['model']}`。",
        f"- 样例：{', '.join(report['case_ids'])}；数据文件 SHA-256：`{report['case_file_sha256']}`。",
        "- 固定合成历史重放并单次回答，不运行完整研究 Agent；每题每组一次。",
        f"- append 是不限预算的完整历史参考，legacy 与 ss 共同目标：{report['conditions']['compressed_target']}；两类不能视作相同预算。",
        "- 所有组无重读工具；SS 的事实字面检查只是线索，不能代替语义评估。",
        "- 发生构建失败的组仍会以保留的上下文回答；其回答正确数不能计作新方案的有效成功。",
        "",
        "| 方案 | 严格匹配正确/已评分 | 构建成功且达目标 | 构建 token | 回答 token | 总 token | 已知是否完整 | 累计耗时（秒） |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for mode, row in report["summary"].items():
        if row["cases"]:
            groups = [g for g in report["groups"] if g["mode"] == mode]

            def known_tokens(answer):
                return sum(
                    call["usage"]["total_tokens"]
                    for group in groups
                    for call in group["calls"]
                    if (call["purpose"] == "answer") == answer
                    and isinstance(call.get("usage"), dict)
                    and type(call["usage"].get("total_tokens")) is int
                )

            retries_tracked = all(
                "transport_retry_count" in call
                for group in groups
                for call in group["calls"]
            )
            completeness = (
                ("是" if row["usage_complete"] else "否")
                if retries_tracked
                else "旧记录未追踪重试"
            )
            target = (
                "不适用"
                if mode == "append"
                else f"{row['applied_and_within_target']}/{row['cases']}"
            )
            lines.append(
                f"| {mode} | {row['answer_correct']}/{row['answer_scored']} | "
                f"{target} | {known_tokens(False)} | {known_tokens(True)} | {row['known_tokens']} | "
                f"{completeness} | {row['duration_seconds']:.2f} |"
            )
    lines += [
        "",
        "## 每题记录",
        "",
        "| 样例 | 方案 | 预期 | 实际 | 正确 | 构建 | 原始/构建后/目标字符 | 已知 token | 耗时（秒） |",
        "|---|---|---|---|---|---|---|---:|---:|",
    ]
    for group in report["groups"]:
        case = by_id[group["case_id"]]
        score = group.get("score") or {}
        construction = group["construction"]
        lines.append(
            f"| {case['id']} | {group['mode']} | {_escape('/'.join(case['accepted']))} | "
            f"{_escape(score.get('answer') if score.get('answer') is not None else '未知')} | "
            f"{score.get('correct', '未知')} | {_escape(construction['status'])} | "
            f"{construction['initial_chars']}/{construction['after_chars']}/{construction['target_chars']} | "
            f"{group['usage']['known_totals']['total_tokens']} | {group['duration_seconds']:.2f} |"
        )
    lines += ["", "## 样例与 SS 选择", ""]
    for id in report["case_ids"]:
        case = by_id[id]
        lines += [
            f"- **{id}（{case['kind']}）**：{case['question']} 标准答案：`{' / '.join(case['accepted'])}`。"
        ]
        for group in report["groups"]:
            if group["case_id"] == id and group["mode"] == "ss":
                info = group["construction"]
                recall = info.get("gold_step_selection_recall")
                checks = info.get("gold_ss_literal_checks", [])
                hits = sum(item.get("all_literals_present") is True for item in checks)
                lines.append(
                    f"  SS 规划选中/近期保护的关键 Step 比例：{recall if recall is not None else '未知'}；"
                    f"关键 Step 摘要的字面线索全匹配：{hits}/{len(checks)}。"
                )
                if info["status"] != "applied":
                    lines.append(
                        f"  构建失败：`{_escape(info.get('error',{}).get('message','unknown'))}`；"
                        "该组答案来自失败后保留的上下文，不代表 SS 方案成功。"
                    )
    lines += [
        "",
        "每次模型请求、回复、usage、SS 目录、规划选择及错误保存在同名 JSON。",
        "精确值匹配不判断自由文本的语义正确性；一次小样本对照不支持总体性能或因果结论。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成上下文对照 Markdown 报告")
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--cases", type=Path, default=Path(__file__).with_name("cases.json")
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    data = load_cases(args.cases)
    import hashlib

    if (
        hashlib.sha256(args.cases.read_bytes()).hexdigest()
        != report["case_file_sha256"]
    ):
        parser.error("样例文件的 SHA-256 与报告不一致，拒绝渲染")
    output = args.output or args.report.with_suffix(".md")
    if output.exists():
        parser.error("输出文件已存在，拒绝覆盖")
    output.write_text(render(report, data["cases"]), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
