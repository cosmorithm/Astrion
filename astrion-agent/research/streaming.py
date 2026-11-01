"""OpenAI 兼容 Chat Completions 流式客户端；仍返回普通 Agent 消息。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from agent import ChatLLM
from response_contract import AnswerStream


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
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return message


class StreamingChatLLM(ChatLLM):
    def __init__(self, key, base_url, model, on_delta):
        super().__init__(key, base_url, model)
        self.on_delta = on_delta
        self.stream_role = "root"

    def fork(self, role):
        child = StreamingChatLLM(self.key, self.base_url, self.model, self.on_delta)
        child.stream_role = role
        return child

    def _post(self, payload, purpose="决策"):
        self.last_usage = None
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
            method="POST",
        )

        def forward(kind, text):
            if purpose == "决策":
                self.on_delta(
                    {"type": "model_delta", "role": self.stream_role, "kind": kind, "text": text}
                )

        assembler = DeltaAssembler(forward)
        done = False
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                for line in response:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    assembler.add(json.loads(data))
        except (urllib.error.URLError, OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"模型流式请求失败：{type(exc).__name__}") from exc
        if not done:
            raise RuntimeError("模型流中断，未收到结束标记")
        self._record_usage(assembler.usage, purpose)
        return assembler.message()
