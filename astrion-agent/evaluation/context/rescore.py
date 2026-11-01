"""Regrade saved responses without another model call or overwriting old results."""

from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .runner import grade, load_cases, save_report, summarize


def rescore(report, cases, followups=None):
    by_id = {case["id"]: case for case in cases}
    for group in report["groups"]:
        if group.get("answer_response") is not None:
            group["score_before_rescore"] = group.get("score")
            group["score"] = grade(
                group["answer_response"], by_id[group["case_id"]]["accepted"]
            )
        for item in group.get("followups", []):
            if followups is None:
                raise ValueError("followup_gold_required")
            gold = followups[group["case_id"]][item["index"] - 1]
            if item["question"] != gold["question"]:
                raise ValueError("followup_question_mismatch")
            if item.get("response") is not None:
                item["score_before_rescore"] = item.get("score")
                item["score"] = grade(item["response"], gold["accepted"])
    report["summary"] = summarize(report["groups"])
    report["regraded_at"] = datetime.now(timezone.utc).isoformat()
    report["grading_method"] = (
        "exact normalized value; JSON string or finite number; string-format compliance separate"
    )
    return report


def main():
    parser = argparse.ArgumentParser(
        description="用更新的规则重算已保存的回答，不重新调用模型"
    )
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--cases", type=Path, default=Path(__file__).with_name("cases.json")
    )
    parser.add_argument(
        "--followups", type=Path, default=Path(__file__).with_name("followups.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，拒绝覆盖")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if (
        hashlib.sha256(args.cases.read_bytes()).hexdigest()
        != report["case_file_sha256"]
    ):
        parser.error("样例哈希不一致，拒绝重评")
    followups = None
    if any(g.get("followups") for g in report["groups"]):
        raw = args.followups.read_bytes()
        if hashlib.sha256(raw).hexdigest() != report["conditions"].get(
            "followups_sha256"
        ):
            parser.error("追问文件哈希不一致，拒绝重评")
        followups = json.loads(raw)["cases"]
    report["regraded_from"] = str(args.report)
    save_report(
        rescore(report, load_cases(args.cases)["cases"], followups), args.output
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
