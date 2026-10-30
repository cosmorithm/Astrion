#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Astrion 的结构化运行事件。

Event 与 Memory 的职责不同：Memory 保存模型下一轮需要看到的消息，Event 只追加
已经发生的运行事实。第一版是旁观日志，不参与恢复或重放。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

EVENT_SCHEMA_VERSION = 1
EVENT_TYPES = frozenset({
    "run.started",
    "run.finished",
    "model.call.started",
    "model.call.finished",
    "model.call.failed",
    "tool.call.started",
    "tool.call.finished",
    "memory.compaction.started",
    "memory.compaction.finished",
    "memory.compaction.failed",
})
EVENT_FIELDS = frozenset({
    "schema_version",
    "event_id",
    "run_id",
    "sequence",
    "type",
    "occurred_at",
    "step",
    "payload",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("事件时钟必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("事件时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_event(event: dict) -> None:
    """校验一个 v1 事件的外壳和 JSON 可序列化性。"""
    if not isinstance(event, dict):
        raise TypeError("event 必须是字典")
    if set(event) != EVENT_FIELDS:
        missing = sorted(EVENT_FIELDS - set(event))
        extra = sorted(set(event) - EVENT_FIELDS)
        raise ValueError(f"事件字段不匹配，缺少 {missing}，多出 {extra}")
    if event["schema_version"] != EVENT_SCHEMA_VERSION:
        raise ValueError(f"不支持的事件版本: {event['schema_version']!r}")
    for key in ("event_id", "run_id", "occurred_at"):
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValueError(f"{key} 必须是非空字符串")
    if type(event["sequence"]) is not int or event["sequence"] <= 0:
        raise ValueError("sequence 必须是正整数")
    if event["type"] not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event['type']!r}")
    if event["step"] is not None and (
        type(event["step"]) is not int or event["step"] <= 0
    ):
        raise ValueError("step 必须是正整数或 null")
    if not isinstance(event["payload"], dict):
        raise ValueError("payload 必须是字典")
    # Event 是可导出的运行事实；不可序列化的数据在进入日志时就应失败。
    json.dumps(event, ensure_ascii=False, allow_nan=False)


class EventEmitter:
    """为一次 run() 生成带顺序号的事件，并交给可选 sink。"""

    def __init__(
        self,
        sink: Callable[[dict], None] | None = None,
        *,
        run_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if sink is not None and not callable(sink):
            raise TypeError("event sink 必须可调用或为 None")
        chosen_run_id = run_id if run_id is not None else f"run_{uuid4().hex}"
        if not isinstance(chosen_run_id, str) or not chosen_run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.run_id = chosen_run_id
        self._sink = sink
        self._clock = clock
        self._sequence = 0

    def emit(self, event_type: str, *, step: int | None = None,
             payload: dict | None = None) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型: {event_type!r}")
        if step is not None and (type(step) is not int or step <= 0):
            raise ValueError("step 必须是正整数或 null")
        if payload is not None and not isinstance(payload, dict):
            raise TypeError("payload 必须是字典或 None")

        sequence = self._sequence + 1
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": f"{self.run_id}:event:{sequence:06d}",
            "run_id": self.run_id,
            "sequence": sequence,
            "type": event_type,
            "occurred_at": _format_time(self._clock()),
            "step": step,
            "payload": copy.deepcopy(payload) if payload is not None else {},
        }
        validate_event(event)
        if self._sink is not None:
            # sink 获得独立副本，不能反向修改已经生成的事实。
            self._sink(copy.deepcopy(event))
        self._sequence = sequence
        return copy.deepcopy(event)


class EventRecorder:
    """内存 sink；严格接收一条 run 的连续事件，并可原子导出 JSON。"""

    def __init__(self):
        self._run_id: str | None = None
        self._events: list[dict] = []
        self._event_ids: set[str] = set()

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    def __call__(self, event: dict) -> None:
        validate_event(event)
        if self._run_id is None:
            self._run_id = event["run_id"]
        elif event["run_id"] != self._run_id:
            raise ValueError("一个 EventRecorder 只能记录一次 run")
        expected = len(self._events) + 1
        if event["sequence"] != expected:
            raise ValueError(f"事件 sequence 不连续：期待 {expected}，收到 {event['sequence']}")
        if event["event_id"] in self._event_ids:
            raise ValueError(f"重复的 event_id: {event['event_id']}")
        self._events.append(copy.deepcopy(event))
        self._event_ids.add(event["event_id"])

    def to_dict(self) -> dict:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "run_id": self._run_id,
            "events": self.events,
        }

    def save(self, path: Path | str) -> Path:
        """原子保存事件日志；导出失败不改变已经完成的 run。"""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                          allow_nan=False) + "\n"
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
                os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
        except BaseException:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise
        return target
