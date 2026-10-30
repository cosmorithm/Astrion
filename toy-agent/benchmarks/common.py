#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基准测试适配层：按 case 注入 messages/tools，记录模型原始响应。

这里不做评分。BFCL 的 AST/state transition、ToolBench 的 pass/win rate 都由
各自官方 harness 负责。默认 agent.run() 不经过这里，现有行为不受影响。
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

ChatCall = Callable[[list[dict], list[dict]], dict]


def _require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value


def _require_dict_list(value, name: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} 必须是对象列表")
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FunctionCallCase:
    """一个函数调用评测样例：消息、该 case 的工具集和可选元数据。"""

    id: str
    category: str
    messages: list[dict]
    tools: list[dict]
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        _require_text(self.id, "id")
        _require_text(self.category, "category")
        _require_dict_list(self.messages, "messages")
        _require_dict_list(self.tools, "tools")
        if not self.messages:
            raise ValueError("messages 不能为空")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是对象")

    @classmethod
    def from_dict(cls, data: dict) -> "FunctionCallCase":
        if not isinstance(data, dict):
            raise ValueError("case 必须是对象")
        return cls(
            id=data.get("id"),
            category=data.get("category"),
            messages=data.get("messages"),
            tools=data.get("tools"),
            metadata=data.get("metadata", {}),
        )


def load_cases(path: Path | str) -> list[FunctionCallCase]:
    """从 JSON 列表读取通用 case；BFCL/ToolBench 适配器负责转换成这个格式。"""
    target = Path(path).expanduser()
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("case 文件必须是对象列表")
    cases = [FunctionCallCase.from_dict(item) for item in data]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case id 不能重复")
    return cases


def run_case(case: FunctionCallCase, chat: ChatCall, *, model: str | None = None) -> dict:
    """运行一个 case，记录请求、原始响应和元数据；不判断对错。"""
    if not isinstance(case, FunctionCallCase):
        raise TypeError("case 必须是 FunctionCallCase")
    if not callable(chat):
        raise TypeError("chat 必须可调用")
    if model is not None and not isinstance(model, str):
        raise ValueError("model 必须是字符串或 None")

    request_messages = copy.deepcopy(case.messages)
    request_tools = copy.deepcopy(case.tools)
    # 给 chat 的副本与最终写入记录的副本分开，避免调用方修改请求。
    response = chat(copy.deepcopy(request_messages), copy.deepcopy(request_tools))
    if not isinstance(response, dict):
        raise TypeError("chat 必须返回字典")

    return {
        "id": case.id,
        "category": case.category,
        "model": model,
        "timestamp": _timestamp(),
        "messages": request_messages,
        "tools": request_tools,
        "response": copy.deepcopy(response),
        "metadata": copy.deepcopy(case.metadata),
    }


def run_cases(
    cases: Iterable[FunctionCallCase],
    chat: ChatCall,
    *,
    limit: int | None = None,
    model: str | None = None,
    continue_on_error: bool = True,
) -> list[dict]:
    """顺序运行多个 case；默认把单个 case 的异常记录为 error 后继续。"""
    items = list(cases)
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit 必须是非负整数或 None")
        items = items[:limit]

    records = []
    for case in items:
        try:
            records.append(run_case(case, chat, model=model))
        except (Exception, SystemExit) as exc:
            if not continue_on_error:
                raise
            records.append({
                "id": getattr(case, "id", None),
                "category": getattr(case, "category", None),
                "model": model,
                "timestamp": _timestamp(),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            })
    return records


def write_jsonl(records: Iterable[dict], path: Path | str) -> Path:
    """把结果写成 JSONL；先完整序列化，避免序列化失败留下半截文件。"""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        for record in records
    )
    target.write_text(text, encoding="utf-8")
    return target


def summarize(records: list[dict]) -> dict:
    """只汇总量和错误，不计算通过率；通过率由官方或专门的 scorer 负责。"""
    categories = Counter()
    errors = []
    for record in records:
        category = record.get("category") or "unknown"
        categories[category] += 1
        if "error" in record:
            errors.append(record.get("id"))
    return {
        "total_cases": len(records),
        "categories": dict(categories),
        "error_count": len(errors),
        "error_cases": errors,
    }
