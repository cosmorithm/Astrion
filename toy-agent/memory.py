#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory.py —— 会话记忆（最小版）

只回答一个问题：上下文快满了，该丢什么、怎么丢。

    · 消息窗口：for_model() 返回当前该发给模型的全部消息
    · 超额压缩：需要摘要时，由调用方传进来一个函数，它不知道模型的存在

刻意不做的事：持久化、统计报表、多窗口、向量检索。先把这一件事做对。

一条硬约束：压缩必须以「轮」为单位切。assistant 的 tool_calls 和它对应的
tool 结果是一对，拆开就是非法消息，API 会直接 400。
"""

from __future__ import annotations

import json
from typing import Callable

SUMMARY_HEADER = "\n\n## 你之前做过的事（自动压缩的摘要）\n"

Summarizer = Callable[[str], str]


class Memory:
    def __init__(
        self,
        system_prompt: str,
        budget_chars: int = 12000,
        keep_recent: int = 4,
        summarizer: Summarizer | None = None,
    ):
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
