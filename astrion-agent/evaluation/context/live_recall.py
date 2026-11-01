"""Replay fixed history, rebuild it, then run the real agent with history tools.

This measures a scoped recall task; the preloaded history is synthetic and is
not represented as a real previous research run.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
from time import perf_counter
from uuid import uuid4

import agent
from context_construction import ContextConstructor
from context_tokenizer import load_token_counter
from context_tools import CONTEXT_SPECS
from evaluation.context.runner import initial_history, load_cases, save_report

BASE = Path(__file__).resolve().parent
NEUTRAL = "整理研究过程和后续工作状态，准备后续研究查询。当前不需要汇总具体实验数字。"


def fixtures():
    raw = (BASE / "live_cases.json").read_bytes()
    spec = json.loads(raw)
    sources = load_cases(BASE / "cases_long.json")
    by_id = {c["id"]: c for c in sources["cases"]}
    cases = []
    for entry in spec["cases"]:
        case = copy.deepcopy(by_id[entry["source_case"]])
        case["id"] = entry["id"]
        case["question"] = NEUTRAL
        if entry.get("swap_steps"):
            first, second = entry["swap_steps"]
            a, b = case["steps"][first - 1], case["steps"][second - 1]
            a["text"], b["text"] = b["text"], a["text"]
        source = case["steps"][entry["source_step"] - 1]["text"]
        if not contains_value(source, entry["expected"]):
            raise ValueError("fixture_answer_not_in_source")
        hits = [
            step["sequence"]
            for step in case["steps"]
            if contains_value(step["text"], entry["expected"])
        ]
        if hits != [entry["source_step"]]:
            raise ValueError("fixture_answer_not_unique")
        cases.append({"spec": entry, "history": case})
    return (
        cases,
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256((BASE / "cases_long.json").read_bytes()).hexdigest(),
    )


def contains_value(text, value):
    # Boundaries avoid 148 matching 1480 and 0.005 matching 0.0005.
    return bool(re.search(r"(?<![\d.])" + re.escape(value) + r"(?![\d.])", text))


def answer_correct(answer, expected):
    if not isinstance(answer, str):
        return False
    cleaned = answer.strip().strip("`").strip()
    return cleaned == expected


def visibility_timeline(trace, expected, initially_visible):
    """Report observed arrival order; do not infer the model's hidden reasoning."""
    if initially_visible:
        return {
            "first_observed": "initial_context",
            "first_decision_index": None,
            "first_tool_index": None,
        }
    decision_index = None
    tool_index = None
    for index, event in enumerate(trace):
        payload = event["payload"]
        if (
            event["type"] == "model.call.started"
            and payload.get("purpose") == "decision"
        ):
            snapshot = payload.get("request_snapshot") or {}
            if contains_value(
                json.dumps(snapshot.get("messages", []), ensure_ascii=False), expected
            ):
                if decision_index is None:
                    decision_index = index
        if (
            event["type"] == "tool.call.finished"
            and payload.get("outcome") == "success"
            and payload.get("name")
            in (
                "search_step_history",
                "read_step_history",
                "search_evidence",
                "read_evidence",
            )
            and contains_value(str(payload.get("content", "")), expected)
        ):
            if tool_index is None:
                tool_index = index
    if tool_index is not None and (
        decision_index is None or tool_index < decision_index
    ):
        first = "tool_result"
    elif decision_index is not None:
        first = "later_decision_context"
    else:
        first = "not_observed"
    return {
        "first_observed": first,
        "first_decision_index": decision_index,
        "first_tool_index": tool_index,
    }


def run_one(fixture, strategy, llm, *, max_steps=10):
    spec, case = fixture["spec"], fixture["history"]
    counter = load_token_counter(getattr(llm, "model", None))
    trace = []
    construction_calls = []
    agent_output = io.StringIO()
    before_usage = dict(llm.usage_totals)
    before_count = llm.usage_calls
    before_unknown = llm.usage_unknown
    started = perf_counter()
    result = {
        "case_id": spec["id"],
        "strategy": strategy,
        "question": spec["question"],
        "expected": spec["expected"],
        "source_step": spec["source_step"],
        "position": spec["position"],
        "construction_calls": construction_calls,
        "trace": trace,
        "status": "running",
    }
    with tempfile.TemporaryDirectory(prefix="astrion-live-recall-") as tmp:
        memory, history, _ = initial_history(case, Path(tmp) / "context.sqlite3")
        memory.system_prompt = agent.SYSTEM_PROMPT
        finish_spec = next(s for s in agent.TOOL_SPECS if s.name == "finish")
        tools = [finish_spec.as_api_tool()] + [s.as_api_tool() for s in CONTEXT_SPECS]
        input_before = counter.count_request(memory.for_model(), tools)
        budget = max(1, int(input_before * 1.25))
        chars_before = memory.chars()
        result["budget"] = {
            "local_input_tokens": input_before,
            "local_budget_tokens": budget,
            "local_target_tokens": budget // 2,
            "initial_chars": chars_before,
            "legacy_target_chars": int(chars_before * 0.625),
            "tokenizer": counter.identifier,
        }
        started_construct = perf_counter()

        def invoke(purpose, messages, *, summarize=False):
            request = {"messages": copy.deepcopy(messages), "tools": []}
            local_count = counter.count_request(messages, [])
            record = {
                "purpose": purpose,
                "request": request,
                "local_input_tokens": local_count,
                "sent": False,
            }
            construction_calls.append(record)
            then = perf_counter()
            output = io.StringIO()
            try:
                if local_count > budget:
                    raise ValueError("local_input_budget_exceeded_before_send")
                record["sent"] = True
                with contextlib.redirect_stdout(output):
                    answer = (
                        {"content": llm.summarize(messages[1]["content"])}
                        if summarize
                        else llm.chat(messages, tools=[])
                    )
                record.update(
                    response=copy.deepcopy(answer), usage=copy.deepcopy(llm.last_usage)
                )
                return answer
            except (Exception, SystemExit) as exc:
                record["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
                raise
            finally:
                record["duration_seconds"] = perf_counter() - then
                record["transport_retries"] = output.getvalue().count("[模型] 请求失败")

        try:
            if strategy == "ss":
                memory.budget_chars = max(
                    chars_before * 2, 1
                )  # character threshold must not masquerade as token budget
                constructor = ContextConstructor(
                    memory,
                    history,
                    lambda purpose, msg: invoke(purpose, msg),
                    model=llm.model,
                    usage=lambda: llm.last_usage,
                    token_counter=counter,
                )
                build = constructor.rebuild(
                    NEUTRAL, tools=tools, input_budget_tokens=budget
                )
                result["construction"] = {"status": "applied", "detail": build}
            elif strategy == "legacy":
                memory.budget_chars = int(chars_before * 0.625)

                def summarize(transcript):
                    return invoke(
                        "legacy_summary",
                        [
                            {"role": "system", "content": agent.SUMMARIZE_PROMPT},
                            {"role": "user", "content": transcript},
                        ],
                        summarize=True,
                    )["content"]

                changed = memory.compact(summarize, force=False)
                result["construction"] = {
                    "status": "applied" if changed else "unchanged",
                    "detail": {
                        "reason": memory.compaction_reason,
                        "layers": memory.layer_reports,
                    },
                }
            else:
                raise ValueError("invalid_strategy")
        except (Exception, SystemExit) as exc:
            result["construction"] = {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
            }
        result["construction"]["duration_seconds"] = perf_counter() - started_construct
        messages = memory.for_model()
        count_after = counter.count_request(messages, tools)
        result["construction"].update(
            local_after_tokens=count_after,
            within_target=count_after <= budget // 2,
            visible_before_question=messages,
            expected_literal_visible=contains_value(
                json.dumps(messages, ensure_ascii=False), spec["expected"]
            ),
        )
        if result["construction"]["status"] != "applied":
            result["status"] = "construction_failed"
            result["answer"] = None
        else:
            # Budget remains enforced. Disable a second character-watermark
            # rebuild so this measures the exact first view and subsequent reads.
            memory.budget_chars = max(chars_before * 4, 100000)
            old_budget = os.environ.get("ASTRION_CONTEXT_INPUT_BUDGET_TOKENS")
            os.environ["ASTRION_CONTEXT_INPUT_BUDGET_TOKENS"] = str(budget)
            try:
                with contextlib.redirect_stdout(agent_output):
                    response = agent.run(
                        NEUTRAL,
                        llm,
                        memory,
                        max_steps=max_steps,
                        resume=True,
                        user_reply=spec["question"]
                        + " 若当前资料不足，可以使用可用工具查找，不要猜测。",
                        tool_specs=[finish_spec],
                        tool_implementations={"finish": lambda answer: answer},
                        event_sink=trace.append,
                        capture_model_input=True,
                    )
                result["status"] = response["status"]
                result["answer"] = response.get("answer")
                result["agent_result"] = response
            except (Exception, SystemExit) as exc:
                result["status"] = "agent_failed"
                result["answer"] = None
                result["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            finally:
                if old_budget is None:
                    os.environ.pop("ASTRION_CONTEXT_INPUT_BUDGET_TOKENS", None)
                else:
                    os.environ["ASTRION_CONTEXT_INPUT_BUDGET_TOKENS"] = old_budget
        result["correct"] = (
            answer_correct(result.get("answer"), spec["expected"])
            if result.get("answer") is not None
            else None
        )
        retrieval = []
        for event in trace:
            if event["type"] == "tool.call.finished" and event["payload"].get(
                "name"
            ) in (
                "search_step_history",
                "read_step_history",
                "search_evidence",
                "read_evidence",
            ):
                p = event["payload"]
                content = p.get("content", "")
                retrieval.append(
                    {
                        "name": p.get("name"),
                        "outcome": p.get("outcome"),
                        "content": content,
                        "expected_literal_observed": contains_value(
                            str(content), spec["expected"]
                        ),
                    }
                )
        result["retrieval"] = retrieval
        result["retrieval_needed_by_literal"] = not result["construction"][
            "expected_literal_visible"
        ]
        result["expected_observed_in_tool"] = any(
            x["expected_literal_observed"] and x["outcome"] == "success"
            for x in retrieval
        )
        result["post_initial_tool_observed_and_correct"] = bool(
            result["retrieval_needed_by_literal"]
            and result["expected_observed_in_tool"]
            and result["correct"]
        )
        result["visibility_timeline"] = visibility_timeline(
            trace, spec["expected"], result["construction"]["expected_literal_visible"]
        )
        result["tool_first_recovery_and_correct"] = bool(
            result["correct"]
            and result["visibility_timeline"]["first_observed"] == "tool_result"
        )
        result["tool_recovery_and_correct"] = result["tool_first_recovery_and_correct"]
        result["archive_snapshot"] = memory.archive.snapshot()
    result["duration_seconds"] = perf_counter() - started
    result["usage"] = {
        "known_totals": {
            k: llm.usage_totals[k] - before_usage.get(k, 0) for k in llm.usage_totals
        },
        "reported_calls": llm.usage_calls - before_count,
        "unknown_calls": llm.usage_unknown - before_unknown,
        "observed_transport_retries": sum(
            c["transport_retries"] for c in construction_calls
        )
        + agent_output.getvalue().count("[模型] 请求失败"),
        "agent_retry_tracking": True,
    }
    # Failed requests and retry attempts may incur unknown usage.
    result["usage"]["complete"] = not (
        result["usage"]["unknown_calls"]
        or result["usage"]["observed_transport_retries"]
        or result["status"] == "agent_failed"
        or any(c.get("error") and c["sent"] for c in construction_calls)
    )
    return result


def markdown(report):
    lines = [
        "# Agent 历史回查对照",
        "",
        f"模型：`{report['model']}`；运行：`{report['run_id']}`。",
        "",
        "固定合成历史经正式构造器或旧压缩处理后，使用真实 Agent Loop、真实历史工具和独立问题。",
        "构造阶段为直接调用构造器，不模拟自动水位触发；任务历史并非真实科研运行。",
        "追问阶段人为放宽字符水位，本地 token 水位仍可触发重构，不能当作工作台默认配置的原样表现。",
        "",
        "| 样例 | 方案 | 位置 | 构建 | 本地 token 后/目标 | 答案预先字面可见 | 回查工具 | 工具中观察到答案 | 最终正确 | 已知 token |",
        "|---|---|---|---|---|---|---:|---|---|---:|",
    ]
    for g in report["groups"]:
        c = g["construction"]
        b = g["budget"]
        lines.append(
            f"| {g['case_id']} | {g['strategy']} | {g['position']} | {c['status']} | {c['local_after_tokens']}/{b['local_target_tokens']} | "
            f"{c['expected_literal_visible']} | {len(g['retrieval'])} | {g['expected_observed_in_tool']} | {g['correct']} | {g['usage']['known_totals']['total_tokens']} |"
        )
    lines += [
        "",
        "答案字面可见不等于语义可用；工具结果含答案也不证明 Agent 正确使用了该证据。",
        "进一步按轨迹检查答案最早出现在初始视图、后续决策视图还是工具结果中；这仍不能推断模型隐藏推理。",
        "严格值匹配不等于科研语义裁判；样例为人工合成，结果不能泛化。",
        "",
    ]
    for g in report["groups"]:
        tracked = g["usage"].get("agent_retry_tracking", False)
        unknown = (
            "未追踪 Agent 内部重试" if not tracked else str(not g["usage"]["complete"])
        )
        timeline = g.get("visibility_timeline") or visibility_timeline(
            g["trace"], g["expected"], g["construction"]["expected_literal_visible"]
        )
        tool_first = bool(
            g.get("correct") and timeline["first_observed"] == "tool_result"
        )
        lines.append(
            f"- {g['case_id']}/{g['strategy']}：状态 `{g['status']}`；回答 `{g.get('answer')}`；最早观察到答案：`{timeline['first_observed']}`；工具先返回且答对：`{tool_first}`；未知用量 `{unknown}`。"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="真实 Agent 对固定合成历史做回查对照")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases, fixture_hash, history_hash = fixtures()
    if args.case:
        lookup = {x["spec"]["id"]: x for x in cases}
        if len(set(args.case)) != len(args.case) or any(
            c not in lookup for c in args.case
        ):
            parser.error("unknown or duplicate case")
        cases = [lookup[c] for c in args.case]
    if args.steps < 1 or args.steps > 50:
        parser.error("steps must be 1..50")
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(
        os.environ.get("AGENT_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    if not key:
        parser.error("缺少模型密钥")
    model = os.environ.get("AGENT_MODEL") or "deepseek-flash"
    base = os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com"
    report = {
        "schema": "context-live-recall-v1",
        "run_id": "live_" + uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "fixture_sha256": fixture_hash,
        "history_sha256": history_hash,
        "max_steps": args.steps,
        "conditions": {
            "tools": ["finish"] + [s.name for s in CONTEXT_SPECS],
            "history": "fixed synthetic",
            "construction": "direct constructor or legacy compaction; same input B",
            "followup": "real agent.run, max 10 steps by default",
            "auto_rebuild_during_followup": "character threshold disabled; token budget retained",
        },
        "groups": [],
    }
    source_root = BASE.parents[1]
    names = [
        "agent.py",
        "memory.py",
        "context_construction.py",
        "context_history.py",
        "context_tools.py",
        "context_tokenizer.py",
        "evaluation/context/live_recall.py",
    ]
    report["source_sha256"] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in names
    }
    try:
        report["git_revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        report["tracked_worktree_dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=source_root,
                text=True,
            )
        )
    except (OSError, subprocess.CalledProcessError):
        report["git_revision"], report["tracked_worktree_dirty"] = None, None
    path = args.output or Path("eval_reports") / (report["run_id"] + ".json")
    if path.exists() or path.with_suffix(".md").exists():
        parser.error("output exists")
    save_report(report, path)
    for fixture in cases:
        for strategy in ("legacy", "ss"):
            print("开始", fixture["spec"]["id"], strategy, file=sys.stderr, flush=True)
            result = run_one(
                fixture, strategy, agent.ChatLLM(key, base, model), max_steps=args.steps
            )
            report["groups"].append(result)
            save_report(report, path)
            print(
                "完成",
                fixture["spec"]["id"],
                strategy,
                "构建",
                result["construction"]["status"],
                "正确",
                result["correct"],
                "字面回查成功",
                result["tool_recovery_and_correct"],
                "已知token",
                result["usage"]["known_totals"]["total_tokens"],
                file=sys.stderr,
                flush=True,
            )
    report_path = path.with_suffix(".md")
    if report_path.exists():
        parser.error("markdown output exists")
    report_path.write_text(markdown(report), encoding="utf-8")
    print("报告:", report_path.resolve())
    print("数据:", path.resolve())


if __name__ == "__main__":
    main()
