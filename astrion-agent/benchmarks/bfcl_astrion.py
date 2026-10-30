#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Astrion 的模型客户端和 Event v1 生成 BFCL 单轮结果。

本模块只接管推理入口。样例加载、函数 schema 转换和最终评分仍来自固定版本的
``bfcl-eval``。它需要由 ``.venv-bfcl`` 的 Python 执行；核心函数可用假数据离线测试。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
from collections import defaultdict
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable

# 直接以文件路径交给 .venv-bfcl/bin/python 时，把 astrion-agent/ 放回模块路径。
SOURCE_ROOT = Path(__file__).resolve().parent.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import agent
from benchmarks.bfcl import load_manifest
from benchmarks.common import FunctionCallCase
from events import EVENT_SCHEMA_VERSION, EventEmitter, EventRecorder
from decision import enhanced_decision


SUPPORTED_CATEGORIES = frozenset({
    "simple_python",
    "multiple",
    "parallel",
    "parallel_multiple",
    "irrelevance",
})
GENERATION_REPORT = "astrion_generation_report.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def _atomic_write_jsonl(path: Path, records: Iterable[dict]) -> None:
    text = "".join(
        json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
        for item in records
    )
    _atomic_write_text(path, text)


def validate_single_turn_manifest(manifest) -> None:
    if not manifest.uses_run_ids:
        raise ValueError("Astrion BFCL v1 只支持固定 ID 的单轮子集，不支持 all-scoring")
    unsupported = sorted(set(manifest.categories) - SUPPORTED_CATEGORIES)
    if unsupported:
        raise ValueError(f"Astrion BFCL v1 不支持这些类别: {unsupported}")


def prepare_cases(
    manifest,
    entry_loader: Callable[[str], list[dict]],
    tool_converter: Callable[[list[dict]], list[dict]],
) -> list[FunctionCallCase]:
    """按 manifest 顺序选择官方样例，并转换成 Astrion 的通用 case。"""
    validate_single_turn_manifest(manifest)
    cases = []
    for category, selected_ids in manifest.categories.items():
        entries = entry_loader(category)
        if not isinstance(entries, list):
            raise TypeError(f"BFCL 类别 {category!r} 没有返回列表")
        by_id = {
            item.get("id"): item
            for item in entries
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        missing = [case_id for case_id in selected_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"BFCL 类别 {category!r} 缺少样例: {missing}")
        for case_id in selected_ids:
            entry = by_id[case_id]
            questions = entry.get("question")
            functions = entry.get("function")
            if (
                not isinstance(questions, list)
                or len(questions) != 1
                or not isinstance(questions[0], list)
            ):
                raise ValueError(f"{case_id} 不是单轮 BFCL 样例")
            if not isinstance(functions, list):
                raise ValueError(f"{case_id} 的 function 必须是列表")
            tools = tool_converter(copy.deepcopy(functions))
            cases.append(FunctionCallCase(
                id=case_id,
                category=category,
                messages=copy.deepcopy(questions[0]),
                tools=tools,
                metadata={"function_count": len(functions)},
            ))
    return cases


def load_official_cases(manifest) -> list[FunctionCallCase]:
    """懒加载固定版本 BFCL 的数据和 OpenAI-compatible tool 转换。"""
    try:
        from bfcl_eval.constants.enums import ModelStyle
        from bfcl_eval.constants.type_mappings import GORILLA_TO_OPENAPI
        from bfcl_eval.model_handler.utils import convert_to_tool
        from bfcl_eval.utils import load_dataset_entry
    except ImportError as exc:
        raise RuntimeError(
            "缺少 bfcl-eval；请使用 astrion-agent/.venv-bfcl/bin/python 执行"
        ) from exc

    def convert(functions: list[dict]) -> list[dict]:
        return convert_to_tool(
            functions, GORILLA_TO_OPENAPI, ModelStyle.OPENAI_COMPLETIONS
        )

    return prepare_cases(manifest, load_dataset_entry, convert)


def normalize_bfcl_result(message: dict):
    """把 OpenAI-compatible assistant 消息转换成 BFCL FC result。"""
    if not isinstance(message, dict):
        raise TypeError("模型响应必须是字典")
    calls = message.get("tool_calls") or []
    if calls:
        if not isinstance(calls, list):
            raise TypeError("tool_calls 必须是列表")
        normalized = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ValueError(f"第 {index + 1} 个 tool call 格式无效")
            function = call["function"]
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not name:
                raise ValueError(f"第 {index + 1} 个 tool call 缺少函数名")
            if not isinstance(arguments, str):
                raise ValueError(f"第 {index + 1} 个 tool call 参数必须是 JSON 字符串")
            normalized.append({name: arguments})
        return normalized
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("无工具调用时，content 必须是字符串或 null")
    return content


def _usage(llm) -> dict | None:
    usage = getattr(llm, "last_usage", None)
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(usage, dict) or any(
        type(usage.get(field)) is not int or usage[field] < 0 for field in fields
    ):
        return None
    return {field: usage[field] for field in fields}


def _event_file_name(case_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id).strip("_.-")[:80]
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
    return f"{readable or 'case'}_{digest}.json"


def run_case(case: FunctionCallCase, llm, event_dir: Path) -> tuple[dict, dict]:
    """执行一个 BFCL 单轮 case，返回官方结果行和 Astrion 运行索引。"""
    recorder = EventRecorder()
    emitter = EventEmitter(recorder)
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / _event_file_name(case.id)
    model_call_id = f"{emitter.run_id}:model:0001"
    common = {
        "model_call_id": model_call_id,
        "purpose": "bfcl_single_turn_decision",
        "model": getattr(llm, "model", None),
    }
    emitter.emit("run.started", payload={
        "execution_path": "astrion_single_turn_adapter",
        "benchmark": "BFCL",
        "case_id": case.id,
        "category": case.category,
        "model": getattr(llm, "model", None),
    })
    emitter.emit("model.call.started", step=1, payload={
        **common,
        "input": {
            "message_count": len(case.messages),
            "message_chars": len(json.dumps(case.messages, ensure_ascii=False)),
            "tool_count": len(case.tools),
        },
    })
    started = perf_counter()
    error = None
    message = None
    result = None
    try:
        message = llm.chat(
            copy.deepcopy(case.messages),
            tools=copy.deepcopy(case.tools),
        )
        latency = perf_counter() - started
        usage = _usage(llm)
        emitter.emit("model.call.finished", step=1, payload={
            **common,
            "duration_seconds": latency,
            "usage": usage,
            "response": message,
        })
        result = normalize_bfcl_result(message)
    except (Exception, SystemExit) as exc:
        latency = perf_counter() - started
        error = {"type": type(exc).__name__, "message": str(exc)}
        if message is None:
            emitter.emit("model.call.failed", step=1, payload={
                **common,
                "duration_seconds": latency,
                "usage": _usage(llm),
                "error": error,
            })
        else:
            # 模型调用已经完成，但适配器无法把响应规范化为 BFCL result。
            emitter.emit("run.finished", payload={
                "status": "adapter_error",
                "steps": 1,
                "answer": None,
                "error": error,
            })
    else:
        emitter.emit("run.finished", payload={
            "status": "response_generated",
            "steps": 1,
            "answer": None,
        })

    recorder.save(event_path)
    usage = _usage(llm)
    official = {
        "id": case.id,
        "result": (
            result
            if error is None
            else f"Error during inference: {error['type']}: {error['message']}"
        ),
        "input_token_count": None if usage is None else usage["prompt_tokens"],
        "output_token_count": None if usage is None else usage["completion_tokens"],
        "latency": latency,
    }
    record = {
        "id": case.id,
        "category": case.category,
        "status": "completed" if error is None else "error",
        "run_id": recorder.run_id,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event_path": str(event_path),
        "latency": latency,
        "token_usage": usage,
        "error": error,
    }
    return official, record


def run_enhanced_case(case: FunctionCallCase, llm, event_dir: Path) -> tuple[dict, dict]:
    """通过 Astrion 共用决策模块完成一轮题目；不执行 BFCL 工具。"""
    recorder = EventRecorder()
    emitter = EventEmitter(recorder)
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / _event_file_name(case.id)
    emitter.emit("run.started", payload={"execution_path": "astrion_enhanced_decision",
                 "benchmark": "BFCL", "case_id": case.id, "category": case.category,
                 "model": getattr(llm, "model", None)})
    calls = []
    started = perf_counter()

    def call_model(messages, tools, purpose):
        step = len(calls) + 1
        call_id = f"{emitter.run_id}:model:{step:04d}"
        common = {"model_call_id": call_id, "purpose": purpose, "model": getattr(llm, "model", None)}
        emitter.emit("model.call.started", step=step, payload={**common, "input": {
            "message_count": len(messages), "message_chars": len(json.dumps(messages, ensure_ascii=False)),
            "tool_count": len(tools)}})
        call_started = perf_counter()
        try:
            response = llm.chat(copy.deepcopy(messages), tools=copy.deepcopy(tools))
        except (Exception, SystemExit) as exc:
            calls.append({"purpose": purpose, "usage": None,
                          "duration_seconds": perf_counter() - call_started, "status": "error"})
            emitter.emit("model.call.failed", step=step, payload={**common,
                         "duration_seconds": calls[-1]["duration_seconds"], "usage": None,
                         "error": {"type": type(exc).__name__, "message": str(exc)}})
            raise
        usage = _usage(llm)
        calls.append({"purpose": purpose, "usage": usage,
                      "duration_seconds": perf_counter() - call_started, "status": "completed"})
        emitter.emit("model.call.finished", step=step, payload={**common,
                     "duration_seconds": calls[-1]["duration_seconds"], "usage": usage,
                     "response": response})
        return response

    decision = None
    error = None
    try:
        decision = enhanced_decision(case.messages, case.tools, call_model)
        result = normalize_bfcl_result(decision["message"])
    except (Exception, SystemExit) as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        result = f"Error during inference: {error['type']}: {error['message']}"
    duration = perf_counter() - started
    unknown = sum(item["usage"] is None for item in calls)
    known = {key: sum(item["usage"][key] for item in calls if item["usage"] is not None)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    usage = known if calls and not unknown else None
    emitter.emit("run.finished", payload={"status": "response_generated" if error is None else "adapter_error",
                 "steps": len(calls), "answer": None, "error": error})
    recorder.save(event_path)
    official = {"id": case.id, "result": result,
                "input_token_count": usage["prompt_tokens"] if usage else None,
                "output_token_count": usage["completion_tokens"] if usage else None,
                "latency": duration}
    record = {"id": case.id, "category": case.category,
              "status": "completed" if error is None else "error",
              "run_id": recorder.run_id, "event_schema_version": EVENT_SCHEMA_VERSION,
              "event_path": str(event_path), "latency": duration, "token_usage": usage,
              "known_token_usage": known, "unknown_usage_calls": unknown,
              "model_call_count": len(calls), "model_calls": calls, "decision": decision,
              "error": error}
    return official, record


def _default_result_path(root: Path, scorer_model: str, category: str) -> Path:
    try:
        from bfcl_eval.utils import (
            get_directory_structure_by_category,
            get_file_name_by_category,
        )
    except ImportError as exc:
        raise RuntimeError("缺少 bfcl-eval，无法确定官方结果路径") from exc
    return (
        root
        / "result"
        / scorer_model.replace("/", "_")
        / get_directory_structure_by_category(category)
        / get_file_name_by_category(category, is_result_file=True)
    )


def generate(
    cases: list[FunctionCallCase],
    llm,
    root: Path,
    *,
    scorer_model: str,
    result_path: Callable[[Path, str, str], Path] = _default_result_path,
    mode: str = "single",
) -> dict:
    """顺序生成结果和每 case Event；不在这里判断正确性。"""
    if mode not in {"single", "enhanced"}:
        raise ValueError("mode 必须是 single 或 enhanced")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    by_category: dict[str, list[dict]] = defaultdict(list)
    initial_by_category: dict[str, list[dict]] = defaultdict(list)
    category_paths: dict[str, Path] = {}
    case_records = []
    event_dir = root / "events"
    started = perf_counter()
    total = len(cases)
    for index, case in enumerate(cases, 1):
        print(f"[Astrion BFCL] {index}/{total} {case.id}", file=sys.stderr)
        official, record = (run_enhanced_case if mode == "enhanced" else run_case)(case, llm, event_dir)
        by_category[case.category].append(official)
        category_path = category_paths.get(case.category)
        if category_path is None:
            category_path = result_path(root, scorer_model, case.category)
            category_paths[case.category] = category_path
        # 每条完成后原子刷新当前类别，便于父进程显示进度；中断时也保留已确认结果。
        _atomic_write_jsonl(category_path, by_category[case.category])
        if mode == "enhanced":
            initial = dict(official)
            decision = record.get("decision")
            if decision is not None:
                try:
                    initial["result"] = normalize_bfcl_result(decision["initial_message"])
                except (ValueError, TypeError) as exc:
                    initial["result"] = f"Error during inference: {type(exc).__name__}: {exc}"
            initial_by_category[case.category].append(initial)
            initial_path = result_path(root / "initial", scorer_model, case.category)
            _atomic_write_jsonl(initial_path, initial_by_category[case.category])
        record["event_path"] = str(Path(record["event_path"]).relative_to(root))
        case_records.append(record)

    result_files = [
        str(category_paths[category].relative_to(root))
        for category in by_category
    ]

    known_input = sum(
        (item.get("known_token_usage") or item.get("token_usage") or {}).get("prompt_tokens", 0)
        for item in case_records
    )
    known_output = sum(
        (item.get("known_token_usage") or item.get("token_usage") or {}).get("completion_tokens", 0)
        for item in case_records
    )
    report = {
        "kind": "bfcl_astrion_generation",
        "status": "completed",
        "execution_path": "astrion_enhanced_decision" if mode == "enhanced" else "astrion_single_turn_adapter",
        "decision_mode": mode,
        "api_model": getattr(llm, "model", None),
        "scorer_model": scorer_model,
        "case_count": total,
        "completed_cases": sum(item["status"] == "completed" for item in case_records),
        "error_count": sum(item["status"] == "error" for item in case_records),
        "duration_seconds": perf_counter() - started,
        "token_usage": {
            "known_input_tokens": known_input,
            "known_output_tokens": known_output,
            "known_total_tokens": known_input + known_output,
            "unknown_cases": sum(item["token_usage"] is None for item in case_records),
            "unknown_calls": sum(item.get("unknown_usage_calls", int(item["token_usage"] is None)) for item in case_records),
        },
        "model_call_count": sum(item.get("model_call_count", 1) for item in case_records),
        "repair_count": sum(bool((item.get("decision") or {}).get("repair_attempted")) for item in case_records),
        "plan_parse_error_count": sum(bool((item.get("decision") or {}).get("plan_error")) for item in case_records),
        "review_parse_error_count": sum(bool((item.get("decision") or {}).get("review_error")) for item in case_records),
        "unchecked_schema_case_count": sum(bool((item.get("decision") or {}).get("final_validation", {}).get("unchecked")) for item in case_records),
        "final_validation_error_count": sum(bool((item.get("decision") or {}).get("final_validation", {}).get("issues")) for item in case_records),
        "initial_project_root": "initial" if mode == "enhanced" else None,
        "result_files": result_files,
        "cases": case_records,
    }
    _atomic_write_json(root / GENERATION_REPORT, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Astrion BFCL 单轮推理 worker")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--scorer-model", required=True)
    parser.add_argument("--mode", choices=("single", "enhanced"), default="single")
    args = parser.parse_args(argv)

    agent.load_env(agent.find_env_files(SOURCE_ROOT))
    key = agent.first_nonempty(
        os.environ.get("AGENT_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    if not key:
        print("错误: 没有可用的模型 API key", file=sys.stderr)
        return 2
    model = args.api_model or os.environ.get("AGENT_MODEL") or "deepseek-chat"
    base_url = args.base_url or os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com"
    manifest = load_manifest(args.manifest)
    cases = load_official_cases(manifest)
    llm = agent.ChatLLM(key=key, base_url=base_url, model=model)
    report = generate(cases, llm, args.output_dir, scorer_model=args.scorer_model, mode=args.mode)
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host += f":{parsed.port}"
    report["effective_base_url"] = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    _atomic_write_json(args.output_dir / GENERATION_REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
