#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory.py —— 会话记忆（最小版）

只回答一个问题：上下文快满了，该丢什么、怎么丢。

    · 消息窗口：for_model() 返回当前该发给模型的全部消息
    · 超额压缩：需要摘要时，由调用方传进来一个函数，它不知道模型的存在

刻意不做的事：文件读写、统计报表、多窗口、向量检索。
序列化交给 to_dict/from_dict，文件由 session.py 负责。

一条硬约束：压缩必须以「轮」为单位切。assistant 的 tool_calls 和它对应的
tool 结果是一对，拆开就是非法消息，API 会直接 400。
"""

from __future__ import annotations

import copy
import json
from typing import Callable

SUMMARY_HEADER = "\n\n## 你之前做过的事（自动压缩的摘要）\n"

Summarizer = Callable[[str], str]
VALID_ROLES = {"user", "assistant", "tool"}


def _clean_tool_calls(raw, index: int) -> list[dict]:
    """校验会话文件里的工具调用，只保留已知字段。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"第 {index + 1} 条 assistant 消息的 tool_calls 必须是非空列表")
    calls = []
    for call in raw:
        if not isinstance(call, dict):
            raise ValueError(f"第 {index + 1} 条消息包含非对象的工具调用")
        call_id = call.get("id")
        function = call.get("function")
        call_type = call.get("type", "function")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError(f"第 {index + 1} 条消息的工具调用缺少 id")
        if not isinstance(function, dict):
            raise ValueError(f"第 {index + 1} 条消息的工具调用缺少 function")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if not isinstance(name, str) or not name:
            raise ValueError(f"第 {index + 1} 条消息的工具调用缺少名称")
        if not isinstance(arguments, str):
            raise ValueError(f"第 {index + 1} 条消息的工具参数必须是字符串")
        if not isinstance(call_type, str) or not call_type:
            raise ValueError(f"第 {index + 1} 条消息的工具调用 type 无效")
        calls.append({
            "id": call_id,
            "type": call_type,
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def _clean_history(raw) -> list[dict]:
    """校验并清洗历史消息，避免把随意 JSON 直接装回 Memory。"""
    if not isinstance(raw, list):
        raise ValueError("history 必须是列表")
    cleaned = []
    for index, message in enumerate(raw):
        if not isinstance(message, dict):
            raise ValueError(f"第 {index + 1} 条消息必须是对象")
        role = message.get("role")
        if role not in VALID_ROLES:
            raise ValueError(f"第 {index + 1} 条消息的 role 无效: {role!r}")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError(f"第 {index + 1} 条消息的 content 必须是字符串或 null")
        if role in {"user", "tool"} and not isinstance(content, str):
            raise ValueError(f"第 {index + 1} 条消息的 content 必须是字符串")

        item = {"role": role, "content": content}
        if role == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = _clean_tool_calls(message["tool_calls"], index)
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"第 {index + 1} 条 tool 消息缺少 tool_call_id")
            item["tool_call_id"] = call_id
        cleaned.append(item)
    return cleaned


class Memory:
    def __init__(
        self,
        system_prompt: str,
        budget_chars: int = 12000,
        keep_recent: int = 4,
        summarizer: Summarizer | None = None,
    ):
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt 必须是非空字符串")
        if type(budget_chars) is not int or budget_chars <= 0:
            raise ValueError("budget_chars 必须是正整数")
        if type(keep_recent) is not int or keep_recent < 1:
            raise ValueError("keep_recent 必须是正整数")
        self.system_prompt = system_prompt
        self.budget_chars = budget_chars
        self.keep_recent = keep_recent      # 压缩时至少保留最近几轮原文
        self.summarizer = summarizer
        self.compactions = 0                # 压过几次，便于观察
        self._history: list[dict] = []
        self._summary = ""

    # ------------------------------------------------------------ 写入

    def add_user(self, text: str) -> None:
        self._history.append({"role": "user", "content": text})

    def add_assistant(self, msg: dict) -> None:
        """只保留必要字段，避免把厂商私有字段（如 reasoning_content）回传。"""
        clean = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        self._history.append(clean)

    def add_tool(self, call_id: str, content: str) -> None:
        self._history.append(
            {"role": "tool", "tool_call_id": call_id, "content": str(content)}
        )

    # ------------------------------------------------------------ 读取

    def for_model(self) -> list[dict]:
        system = self.system_prompt
        if self._summary:
            system += SUMMARY_HEADER + self._summary
        return [{"role": "system", "content": system}, *self._history]

    def chars(self) -> int:
        """用字符数当 token 的代理指标。中英混排大约 1 token ≈ 1.5~2 字符。"""
        return len(json.dumps(self.for_model(), ensure_ascii=False))

    def __len__(self) -> int:
        return len(self._history)

    def last_user(self) -> str | None:
        """返回最近一条用户消息；压缩后它可能已经被移入摘要。"""
        for message in reversed(self._history):
            if message["role"] == "user":
                return message["content"]
        return None

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        """导出可 JSON 序列化的完整记忆状态。"""
        return {
            "system_prompt": self.system_prompt,
            "budget_chars": self.budget_chars,
            "keep_recent": self.keep_recent,
            "summary": self._summary,
            "compactions": self.compactions,
            "history": copy.deepcopy(self._history),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        *,
        system_prompt: str | None = None,
        budget_chars: int | None = None,
        keep_recent: int | None = None,
    ) -> "Memory":
        """从会话文件恢复记忆；显式传入的配置优先于文件中的旧值。"""
        if not isinstance(data, dict):
            raise ValueError("记忆数据必须是对象")
        history = _clean_history(data.get("history"))
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("摘要必须是字符串")
        compactions = data.get("compactions", 0)
        if type(compactions) is not int or compactions < 0:
            raise ValueError("compactions 必须是非负整数")

        prompt = system_prompt if system_prompt is not None else data.get("system_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("system_prompt 必须是非空字符串")
        budget = budget_chars if budget_chars is not None else data.get("budget_chars", 12000)
        keep = keep_recent if keep_recent is not None else data.get("keep_recent", 4)

        memory = cls(prompt, budget_chars=budget, keep_recent=keep)
        memory._history = history
        memory._summary = summary
        memory.compactions = compactions
        problems = memory.check()
        if problems:
            raise ValueError("会话历史不自洽: " + "；".join(problems))
        return memory

    # ------------------------------------------------------------ 分轮与自检

    def units(self) -> list[list[dict]]:
        """切成不可再分的最小单位：一条 assistant（带 tool_calls）+ 它的 tool 结果。"""
        units: list[list[dict]] = []
        open_unit = False
        for m in self._history:
            if m["role"] == "tool" and open_unit and units:
                units[-1].append(m)
                continue
            units.append([m])
            open_unit = m["role"] == "assistant" and bool(m.get("tool_calls"))
        return units

    def check(self) -> list[str]:
        """检查 tool_call 配对是否合法。返回空列表表示健康。"""
        problems: list[str] = []
        pending: list[str] = []
        for m in self._history:
            if m["role"] == "assistant" and m.get("tool_calls"):
                if pending:
                    problems.append(f"上一批工具调用没有结果: {pending}")
                pending = [c["id"] for c in m["tool_calls"]]
            elif m["role"] == "tool":
                cid = m.get("tool_call_id")
                if cid in pending:
                    pending.remove(cid)
                else:
                    problems.append(f"多出来的 tool 消息: {cid}")
            elif m["role"] == "assistant" and pending:
                problems.append(f"工具调用被说话打断: {pending}")
                pending = []
        if pending:
            problems.append(f"未回填的工具调用: {pending}")
        return problems

    # ------------------------------------------------------------ 压缩

    def needs_compaction(self) -> bool:
        return self.chars() > self.budget_chars

    def compact(self, summarizer: Summarizer | None = None) -> bool:
        """把最旧的若干轮压成一段摘要，返回是否真的压了。"""
        fn = summarizer or self.summarizer
        if fn is None:
            raise RuntimeError("没有提供 summarizer，无法压缩")

        units = self.units()
        if len(units) <= 1:
            return False        # 只剩一轮，没得压

        # 保留轮数按预算动态收缩：固定保留 4 轮，遇到大文件照样爆。
        # 至少留 1 轮原文，否则等于把刚发生的事也塞进摘要。
        keep = min(self.keep_recent, len(units) - 1)
        while keep > 1 and self._size(units[-keep:], allowance=400) > self.budget_chars:
            keep -= 1

        old, keep_units = units[:-keep], units[-keep:]
        transcript = self._render(old)
        if self._summary:
            # 摘要的摘要：把上一次的结论一起喂进去，避免越压越丢
            transcript = "【更早的摘要】\n" + self._summary + "\n\n【新增的旧对话】\n" + transcript

        self._summary = fn(transcript).strip()
        self._history = [m for unit in keep_units for m in unit]
        self.compactions += 1

        problems = self.check()
        if problems:
            raise RuntimeError(f"压缩后消息不自洽: {problems}")
        return True

    def _size(self, units: list[list[dict]], allowance: int = 0) -> int:
        """估算「只保留这些轮」之后的消息体积。

        allowance 是给即将生成的摘要预留的余量，否则压完发现还是超。
        """
        flat = [m for unit in units for m in unit]
        probe = [{"role": "system", "content": self.system_prompt}, *flat]
        return len(json.dumps(probe, ensure_ascii=False)) + allowance

    @staticmethod
    def _render(units: list[list[dict]]) -> str:
        lines: list[str] = []
        for unit in units:
            for m in unit:
                if m["role"] == "user":
                    lines.append(f"用户: {m.get('content')}")
                elif m["role"] == "assistant":
                    text = (m.get("content") or "").strip()
                    if text:
                        lines.append(f"助手: {text}")
                    for call in m.get("tool_calls") or []:
                        fn = call["function"]
                        lines.append(f"  调用 {fn['name']}({fn['arguments']})")
                else:
                    lines.append(f"  结果: {m.get('content')}")
        return "\n".join(lines)
