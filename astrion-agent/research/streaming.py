"""OpenAI 兼容 Chat Completions 流式客户端；仍返回普通 Agent 消息。"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

from agent import ChatLLM, RETRIABLE_HTTP_STATUS, retry_after_seconds
from response_contract import AnswerStream


class ModelRequestError(RuntimeError):
    """模型请求失败；保留日志、事件和界面判断所需的分类信息。

    code      失败分类（网络 / HTTP / 流中断 / 响应未完成）
    retriable 是否值得重试（请求已经发出可见内容后不再重试，避免重复输出）
    status    上游 HTTP 状态码（有响应时）
    reason    不含凭据的底层原因，供后端日志和事件使用
    attempts  本次共尝试了几次（重试耗尽时大于 1）
    """

    def __init__(self, message, *, code="model_request_failed", retriable=False,
                 status=None, reason=None, attempts=1, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retriable = bool(retriable)
        self.status = status
        self.reason = reason
        self.attempts = attempts
        self.retry_after = retry_after

    def detail(self) -> dict:
        return {key: value for key, value in (
            ("code", self.code), ("retriable", self.retriable), ("status", self.status),
            ("reason", self.reason), ("attempts", self.attempts),
        ) if value is not None}


def partial_answer(arguments: str) -> str:
    """读取尚未结束的 finish.answer JSON 字符串，遇到未完成的转义就等待。"""
    match = re.match(r'^\s*\{\s*"answer"\s*:\s*"', arguments)
    if not match:
        return ""
    rest = arguments[match.end() :]
    raw = ""
    escaped = False
    for char in rest:
        if char == '"' and not escaped:
            break
        raw += char
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    for size in range(len(raw), max(-1, len(raw) - 12), -1):
        try:
            text = json.loads('"' + raw[:size] + '"')
            # 不输出不完整的 Unicode 代理项。
            text.encode("utf-8")
            return text
        except (ValueError, UnicodeError):
            continue
    return ""


class DeltaAssembler:
    """把分块组装回消息；只暴露普通内容和 finish 参数中的暂定答案。"""

    def __init__(self, emit):
        self.emit = emit
        self.content = ""
        self.reasoning_content = ""
        self.calls = {}
        self.answers = {}
        self.usage = None
        self.finish_reason = None
        self.content_stream = AnswerStream(lambda text: self.emit("content", text))
        self.answer_streams = {}

    def add(self, chunk):
        if chunk.get("usage") is not None:
            self.usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            self.reasoning_content += delta.get("reasoning_content") or ""
            text = delta.get("content") or ""
            if text:
                self.content += text
                self.content_stream.add(text)
            for part in delta.get("tool_calls") or []:
                index = part["index"]
                item = self.calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if part.get("id"):
                    item["id"] = part["id"]
                if part.get("type"):
                    item["type"] = part["type"]
                function = part.get("function") or {}
                if function.get("name"):
                    item["function"]["name"] += function["name"]
                item["function"]["arguments"] += function.get("arguments") or ""
                if item["function"]["name"] == "finish":
                    answer = partial_answer(item["function"]["arguments"])
                    previous = self.answers.get(index, "")
                    if answer.startswith(previous) and len(answer) > len(previous):
                        stream = self.answer_streams.setdefault(
                            index, AnswerStream(lambda text: self.emit("answer", text))
                        )
                        stream.add(answer[len(previous) :])
                        self.answers[index] = answer

    def message(self):
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise RuntimeError(f"模型响应未完成：{self.finish_reason}")
        self.content_stream.finish()
        for stream in self.answer_streams.values():
            stream.finish()
        message = {"role": "assistant", "content": self.content or None}
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return message


class StreamingChatLLM(ChatLLM):
    """流式决策客户端：瞬时网络失败自动退避重试，并保留可诊断的失败原因。"""

    timeout = 120

    def __init__(self, key, base_url, model, on_delta):
        super().__init__(key, base_url, model)
        self.on_delta = on_delta
        self.stream_role = "root"

    def fork(self, role):
        child = StreamingChatLLM(self.key, self.base_url, self.model, self.on_delta)
        child.stream_role = role
        child.retry_delays = self.retry_delays
        return child

    def _post(self, payload, purpose="决策"):
        self.last_usage = None
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        attempts = len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._request(payload, purpose)
            except ModelRequestError as exc:
                exc.attempts = attempt
                if not exc.retriable or attempt == attempts:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else self.retry_delays[attempt - 1]
                print(f"  [模型] 请求失败（{exc.reason or exc.code}），{delay:.1f}s 后重试"
                      f"（第 {attempt + 1}/{attempts} 次）", file=sys.stderr, flush=True)
                self.on_delta({"type": "model_retry", "role": self.stream_role,
                               "attempt": attempt + 1, "attempts": attempts,
                               "delay_seconds": round(delay, 1), "reason": exc.reason or exc.code})
                self._sleep(delay)

    def _request(self, payload, purpose):
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
            method="POST",
        )
        emitted = []

        def forward(kind, text):
            emitted.append(text)
            if purpose == "决策":
                self.on_delta(
                    {"type": "model_delta", "role": self.stream_role, "kind": kind, "text": text}
                )

        assembler = DeltaAssembler(forward)
        done = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    assembler.add(json.loads(data))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise ModelRequestError(
                f"模型请求失败：HTTP {exc.code}",
                code="model_http_error",
                status=exc.code,
                retriable=exc.code in RETRIABLE_HTTP_STATUS and not emitted,
                reason=f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}",
                retry_after=retry_after_seconds(getattr(exc, "headers", None)),
            ) from exc
        except (urllib.error.URLError, OSError, UnicodeError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            raise ModelRequestError(
                f"模型流式请求失败：{type(exc).__name__}（{reason}）",
                code="model_network_error",
                retriable=not emitted,
                reason=f"{type(exc).__name__}: {reason}",
            ) from exc
        if not done:
            raise ModelRequestError(
                "模型流中断，未收到结束标记",
                code="model_stream_incomplete",
                retriable=not emitted,
                reason="stream ended before [DONE]",
            )
        self._record_usage(assembler.usage, purpose)
        try:
            return assembler.message()
        except RuntimeError as exc:
            raise ModelRequestError(
                str(exc), code="model_response_incomplete", retriable=False, reason=str(exc)
            ) from exc
