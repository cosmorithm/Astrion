"""模型调用的预算校验与运行事件记录。"""
from __future__ import annotations

from time import perf_counter


def error_data(exc: BaseException) -> dict:
    """保留模型异常的可观测分类，供失败事件和压缩事件共同使用。"""
    data = {"type": type(exc).__name__, "message": str(exc)}
    for key in ("code", "retriable", "status", "attempts"):
        value = getattr(exc, key, None)
        if value is not None:
            data[key] = value
    return data


class ModelCallRecorder:
    """一次 Agent 运行内的调用编号、事件及本地输入预算。"""

    def __init__(self, llm, run_id, emit, *, token_counter=None,
                 input_budget_tokens=None, capture_model_input=False, clock=perf_counter):
        self.llm = llm
        self.run_id = run_id
        self.emit = emit
        self.token_counter = token_counter
        self.input_budget_tokens = input_budget_tokens
        self.capture_model_input = capture_model_input
        self.clock = clock
        self.call_count = 0

    def call(self, fn, *, purpose, step, input_metadata, request_snapshot=None):
        self.call_count += 1
        common = {
            "model_call_id": f"{self.run_id}:model:{self.call_count:04d}",
            "purpose": purpose,
            "model": getattr(self.llm, "model", None),
        }
        local_tokens = None
        if (self.token_counter is not None and isinstance(request_snapshot, dict)
                and isinstance(request_snapshot.get('messages'), list)):
            local_tokens = self.token_counter.count_request(
                request_snapshot['messages'], request_snapshot.get('tools') or []
            )
        metadata = dict(input_metadata)
        if local_tokens is not None:
            metadata.update(local_input_tokens=local_tokens,
                            local_input_budget_tokens=self.input_budget_tokens,
                            token_counter=self.token_counter.identifier)
        payload = {**common, "input": metadata}
        if self.capture_model_input and request_snapshot is not None:
            payload["request_snapshot"] = request_snapshot
        self.emit("model.call.started", step=step, payload=payload)
        started = self.clock()
        try:
            if self.token_counter is not None:
                if not isinstance(request_snapshot, dict) or not isinstance(request_snapshot.get('messages'), list):
                    raise ValueError('context_request_snapshot_required_for_token_budget')
                if local_tokens > self.input_budget_tokens:
                    raise ValueError(f'context_input_token_budget_exceeded: {local_tokens}>{self.input_budget_tokens}')
            response = fn()
        except BaseException as exc:
            usage = getattr(self.llm, "last_usage", None)
            if not isinstance(usage, dict):
                usage = None
            self.emit("model.call.failed", step=step, payload={
                **common, "duration_seconds": self.clock() - started,
                "usage": usage, "error": error_data(exc),
            })
            raise
        usage = getattr(self.llm, "last_usage", None)
        if not isinstance(usage, dict):
            usage = None
        self.emit("model.call.finished", step=step, payload={
            **common, "duration_seconds": self.clock() - started,
            "usage": usage, "response": response,
        })
        return response
