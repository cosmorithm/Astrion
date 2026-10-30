#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session.py —— 把会话记忆、任务和运行状态保存为 JSON。

只做三件事：保存、加载、判断能否继续。真正的消息结构由 Memory 负责。

保存点放在“一整轮工具结果都写回 Memory 之后”，因此文件里的历史始终能
通过 Memory.check()。如果进程在模型请求或一批工具执行中途被终止，恢复点
最多回退到上一轮结束，那一轮工具可能会被重新执行。

写入使用“临时文件 + fsync + os.replace”原子替换，避免中断留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory import Memory

SESSION_VERSION = 1
RUN_STATUSES = {"running", "completed", "no_tool_call", "step_limit", "error"}
# 这两种状态说明任务没有正常收尾，重跑同一任务时应当继续，而不是重开。
RESUMABLE_STATUSES = frozenset({"running", "step_limit"})


@dataclass(frozen=True)
class Session:
    """一次会话文件的内容。memory 是已经恢复好的 Memory 实例。"""

    path: Path
    memory: Memory
    task: str
    status: str
    steps: int
    model: str | None = None
    saved_at: str | None = None

    def is_resumable(self, task: str) -> bool:
        """同一任务且上次没有正常结束，才按“继续”处理。"""
        return self.status in RESUMABLE_STATUSES and self.task == task


def _validate_status(status: str) -> None:
    if not isinstance(status, str) or status not in RUN_STATUSES:
        raise ValueError(f"无效的会话状态: {status!r}")


def save_session(
    path: Path | str,
    *,
    memory: Memory,
    task: str,
    status: str,
    steps: int,
    model: str | None = None,
) -> Path:
    """原子保存会话；返回实际写入的绝对路径。"""
    if not isinstance(memory, Memory):
        raise TypeError("memory 必须是 Memory 实例")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 必须是非空字符串")
    _validate_status(status)
    if type(steps) is not int or steps < 0:
        raise ValueError("steps 必须是非负整数")
    if model is not None and not isinstance(model, str):
        raise ValueError("model 必须是字符串或 null")

    problems = memory.check()
    if problems:
        raise ValueError("记忆不自洽，拒绝保存: " + "；".join(problems))

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SESSION_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": task,
        "status": status,
        "steps": steps,
        "model": model,
        "memory": memory.to_dict(),
    }
    # 先完整序列化，确认可写再落盘；失败时不会留下半成品。
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"

    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            # 会话里可能有文件内容和工具输出，默认只给当前用户读写。
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
    return target


def load_session(
    path: Path | str,
    *,
    system_prompt: str | None = None,
    budget_chars: int | None = None,
    keep_recent: int | None = None,
) -> Session:
    """读取并校验会话文件；损坏、版本不符或历史不自洽都会抛 ValueError。"""
    target = Path(path).expanduser().resolve()
    text = target.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"会话文件不是有效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("会话文件必须是 JSON 对象")

    version = data.get("version")
    if type(version) is not int or version != SESSION_VERSION:
        raise ValueError(f"不支持的会话版本: {version!r}")

    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("会话缺少有效的 task")
    status = data.get("status")
    _validate_status(status)
    steps = data.get("steps")
    if type(steps) is not int or steps < 0:
        raise ValueError("会话的 steps 必须是非负整数")
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("会话的 model 必须是字符串或 null")
    saved_at = data.get("saved_at")
    if saved_at is not None and not isinstance(saved_at, str):
        raise ValueError("会话的 saved_at 必须是字符串或 null")

    memory_data = data.get("memory")
    if not isinstance(memory_data, dict):
        raise ValueError("会话缺少 memory 对象")
    memory = Memory.from_dict(
        memory_data,
        system_prompt=system_prompt,
        budget_chars=budget_chars,
        keep_recent=keep_recent,
    )
    return Session(
        path=target,
        memory=memory,
        task=task,
        status=status,
        steps=steps,
        model=model,
        saved_at=saved_at,
    )
