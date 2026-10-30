#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Astrion Agent —— 以智慧，奔赴星海。
Intelligence bound for the stars.

一个可以完整看懂的最小智能体（零依赖，标准库实现）。

它只做一件事：把「模型 + 工具 + 循环」跑通，并把每一步打印出来，
让你能亲眼看见 ReAct 的三段式：think → act → obs。

用法:
    python3 agent.py "看看 data.txt 里数字的和是多少"   # 调真模型
    python3 agent.py "任务" --session session.json      # 保存并在中断后恢复

配置（优先级从高到低）:
    1. 命令行    --model / --base-url
    2. 环境变量  AGENT_API_KEY / AGENT_BASE_URL / AGENT_MODEL
                 （也认 DEEPSEEK_API_KEY、OPENAI_API_KEY 作为 key 的别名）
    3. .env 文件 从脚本所在目录起向上找，最多 3 层，越近的优先

也就是说：.env 里写一次，之后直接 python3 agent.py "任务" 就行。
"""

from __future__ import annotations

import argparse
import ast
import json
import operator as _op
import os
import sys
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from time import perf_counter
from typing import Callable

from events import EventEmitter
from memory import Memory
from session import load_session, save_session, validate_task_plan
from tooling import ToolCall, ToolRegistry, ToolResult, ToolSpec

# ---------------------------------------------------------------- 终端样式

TTY = sys.stdout.isatty()


def _wrap(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


def dim(s):   return _wrap("2", s)
def bold(s):  return _wrap("1", s)
def cyan(s):  return _wrap("36", s)
def green(s): return _wrap("32", s)
def red(s):   return _wrap("31", s)


# ---------------------------------------------------------------- .env 读取
# 只用标准库，不引入 python-dotenv。逻辑很短，值得逐行看懂。


def find_env_files(start: Path, max_up: int = 3) -> list[Path]:
    """从 start 向上找 .env，返回由近到远的列表。

    这样无论 .env 放在 astrion-agent/ 还是上一层的 Multi Agent/，都能被找到。
    """
    found: list[Path] = []
    cur = start.resolve()
    for _ in range(max_up + 1):
        cand = cur / ".env"
        if cand.is_file():
            found.append(cand)
        if cur.parent == cur:      # 到根了
            break
        cur = cur.parent
    return found


def load_env(files: list[Path]) -> list[Path]:
    """解析 KEY=VALUE 写进 os.environ。

    规则：已经存在的环境变量不会被覆盖 —— 和 python-dotenv 的默认行为一致，
    这样 shell 里临时 export 的值总是赢过文件。
    """
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip().strip('"').strip("'")     # 去掉包裹的引号
            if k and k not in os.environ:
                os.environ[k] = v
    return files


def first_nonempty(*values) -> str:
    for v in values:
        if v and v.strip():
            return v.strip()
    return ""


def mask(secret: str) -> str:
    """打印密钥时永远脱敏。日志会被人截图、贴进 issue，这一步不能省。"""
    return secret[:6] + "…" + secret[-4:] if len(secret) >= 12 else "***"


# ---------------------------------------------------------------- 沙箱

SANDBOX = Path(__file__).resolve().parent / "sandbox"


def safe_path(raw: str, *, sandbox: Path | None = None) -> Path:
    """把路径限制在本次沙箱内；默认使用项目的 sandbox/。"""
    root = (sandbox if sandbox is not None else SANDBOX).resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"路径越界，只允许访问 {root}")
    return target


# ---------------------------------------------------------------- 工具实现


def list_files(path: str = ".", *, sandbox: Path | None = None) -> str:
    p = safe_path(path, sandbox=sandbox)
    if not p.exists():
        raise FileNotFoundError(f"{path} 不存在")
    entries = []
    for child in sorted(p.iterdir()):
        entries.append(child.name + ("/" if child.is_dir() else ""))
    return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)


def read_file(path: str, *, sandbox: Path | None = None) -> str:
    p = safe_path(path, sandbox=sandbox)
    if not p.is_file():
        raise FileNotFoundError(f"{path} 不是文件")
    text = p.read_text(encoding="utf-8")
    if len(text) > 8000:
        text = text[:8000] + f"\n...（已截断，原长 {len(text)} 字符）"
    return json.dumps({"path": path, "content": text}, ensure_ascii=False)


def write_file(path: str, content: str, *, sandbox: Path | None = None) -> str:
    p = safe_path(path, sandbox=sandbox)
    if len(content) > 100_000:
        raise ValueError("内容太大")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return json.dumps({"ok": True, "path": path, "bytes": len(content.encode())}, ensure_ascii=False)


_BINOPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("指数太大")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    raise ValueError("只支持数字和 + - * / // % ** 和括号")


def calculator(expression: str) -> str:
    if len(expression) > 200:
        raise ValueError("表达式太长")
    value = _eval_node(ast.parse(expression, mode="eval"))
    return json.dumps({"expression": expression, "result": value}, ensure_ascii=False)


def finish(answer: str) -> str:
    """终止信号。把它做成工具，比让模型'自己说完了'可靠得多。"""
    return answer


def ask_user(question: str) -> str:
    """暂停并向用户询问必要信息。"""
    return question


# ---------------------------------------------------------------- 工具清单
# schema 是给模型看的说明书；TOOL_IMPL 是真正执行的代码。
# 两者分开，是为了将来能换成远程工具、加权限检查而不动模型那一侧。

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出 sandbox 目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对 sandbox 的路径，默认 '.'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 sandbox 内一个文本文件的全部内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入 sandbox 内的文件，会覆盖已有文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个算术表达式，例如 '3+1+4' 或 '(2+3)*4'",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "算术表达式"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "任务完成时调用，把最终结论交给用户。调用它之后循环结束",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "给用户的最终回答"}},
                "required": ["answer"],
            },
        },
    },
]

TOOL_IMPL = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "calculator": calculator,
    "finish": finish,
    "ask_user": ask_user,
}

# 多目标计划是模型声明的工作清单，不是对任务结果的独立验收。
TOOLS.insert(-1, {
    "type": "function", "function": {
        "name": "update_task_plan",
        "description": "为多目标任务记录完整子任务清单并更新状态；完成状态必须有工具结果或其他事实依据",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {
                    "goal": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["pending", "completed"]},
                }, "required": ["goal", "status"], "additionalProperties": False,
            }},
        }, "required": ["items"], "additionalProperties": False},
    },
})
TOOLS.insert(-1, {
    "type": "function", "function": {
        "name": "ask_user", "description": "缺少必要信息时暂停任务并向用户询问",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "minLength": 1},
        }, "required": ["question"], "additionalProperties": False},
    },
})

_TOOL_OUTPUTS = {
    "list_files": "目录路径和文件列表", "read_file": "文件路径和文本内容（可能截断）",
    "write_file": "写入是否成功、文件路径和字节数", "calculator": "算术表达式和计算结果",
    "finish": "提交给用户的最终回答", "ask_user": "等待用户回答的问题",
    "update_task_plan": "本次任务的子目标及其声明状态",
}
for _tool in TOOLS:
    _tool["function"]["parameters"].setdefault("additionalProperties", False)
TOOL_SPECS = [ToolSpec(
    name=tool["function"]["name"], description=tool["function"]["description"],
    input_schema=tool["function"]["parameters"],
    output_description=_TOOL_OUTPUTS[tool["function"]["name"]],
    terminal=tool["function"]["name"] in {"finish", "ask_user"},
) for tool in TOOLS]
TOOLS = [spec.as_api_tool() for spec in TOOL_SPECS]


def tools_for_sandbox(sandbox: Path) -> dict:
    """为一次运行绑定独立文件目录，不改变全局 SANDBOX。"""
    return {
        **TOOL_IMPL,
        "list_files": partial(TOOL_IMPL["list_files"], sandbox=sandbox),
        "read_file": partial(TOOL_IMPL["read_file"], sandbox=sandbox),
        "write_file": partial(TOOL_IMPL["write_file"], sandbox=sandbox),
    }

SYSTEM_PROMPT = """你是 Astrion Agent，只能通过工具来观察和改变 sandbox/ 目录。

规则：
1. 一次只做一个动作，等观察结果回来再决定下一步。
2. 不要凭空猜测文件内容，先读。
3. 任务完成时调用 finish 工具。
4. 用中文思考和回答。
5. 多目标任务先用 update_task_plan 记录各子目标；有依据地更新状态，全部完成后才调用 finish。清单是工作记录，不是验收。
6. 只使用能提供当前子目标所需结果的工具；工具不适用时说明限制。不要为了使用工具而编造参数。
7. 缺少必要信息且无法可靠推导时调用 ask_user，等待用户补充；可选参数有默认值时尽量省略。"""

SUMMARIZE_PROMPT = """你在压缩一段智能体的执行记录。
把下面的内容写成不超过 200 字的备忘，必须保留：
1. 用户的任务目标
2. 已完成的操作和结果（文件名、数字、结论）
3. 还没做完的事
只输出备忘本身，不要评论。"""


# ---------------------------------------------------------------- 模型客户端


class ChatLLM:
    """OpenAI 兼容的 /chat/completions 客户端（DeepSeek、OpenAI、通义等都适用）。"""

    def __init__(self, key: str, base_url: str, model: str):
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.model = model
        # 累计服务端报告的实际用量；不拿字符数冒充 token 数。
        self.usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.usage_calls = 0
        self.usage_unknown = 0
        # 最近一次调用的服务端 usage，供结构化运行事件读取；未知时明确为 None。
        self.last_usage: dict | None = None

    def _record_usage(self, usage: dict | None, purpose: str) -> None:
        """记录一次响应的用量。缺失或不完整时标记未知，不按零计算。"""
        self.usage_calls += 1
        valid = isinstance(usage, dict) and all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in self.usage_totals
        )
        if valid:
            self.last_usage = {key: usage[key] for key in self.usage_totals}
            for key in self.usage_totals:
                self.usage_totals[key] += usage[key]
            print(dim(f"  [token][{purpose}] 本次：输入 {usage['prompt_tokens']}"
                      f" / 输出 {usage['completion_tokens']} / 合计 {usage['total_tokens']}"))
        else:
            self.last_usage = None
            self.usage_unknown += 1
            print(dim(f"  [token][{purpose}] 本次：未知（API 未返回完整有效的 usage）"))

        if self.usage_calls == self.usage_unknown:
            print(dim(f"  [token] 累计：未知（{self.usage_unknown} 次调用用量未知）"))
            return

        label = "已知累计" if self.usage_unknown else "累计"
        note = f"（另有 {self.usage_unknown} 次调用用量未知，未计入）" if self.usage_unknown else ""
        totals = self.usage_totals
        print(dim(f"  [token] {label}：输入 {totals['prompt_tokens']}"
                  f" / 输出 {totals['completion_tokens']} / 合计 {totals['total_tokens']}{note}"))

    def _post(self, payload: dict, purpose: str = "决策") -> dict:
        # 请求失败时不能沿用上一次成功调用的 usage。
        self.last_usage = None
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise SystemExit(red(f"API 报错 {e.code}: {body}"))
        except urllib.error.URLError as e:
            raise SystemExit(red(f"连不上 API: {e.reason}"))
        self._record_usage(data.get("usage"), purpose)
        return data["choices"][0]["message"]

    def chat(self, messages: list, tools: list | None = None) -> dict:
        """发送一次决策请求；tools=None 时沿用默认工具集。

        BFCL/ToolBench 这类基准需要按 case 注入工具，所以允许显式传入。
        tools=[] 表示本次不带工具，请求中省略 tools 和 tool_choice。
        默认调用仍然发送 TOOLS，行为与之前一致。
        """
        if tools is not None and not isinstance(tools, list):
            raise TypeError("tools 必须是列表或 None")
        chosen = TOOLS if tools is None else tools
        payload = {
            "model": self.model,
            "messages": messages,
        }
        if chosen:
            payload["tools"] = chosen
            payload["tool_choice"] = "auto"
        payload["temperature"] = 0
        return self._post(payload)

    def summarize(self, transcript: str) -> str:
        """把一段旧对话压成备忘。这次不带 tools —— 压缩不是决策。"""
        msg = self._post({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0,
        }, purpose="记忆摘要")
        return (msg.get("content") or "").strip()


# ---------------------------------------------------------------- 主循环


def run(
    task: str,
    llm: ChatLLM,
    memory: Memory,
    max_steps: int = 10,
    *,
    sandbox: Path | None = None,
    event_sink: Callable[[dict], None] | None = None,
    session_path: Path | str | None = None,
    resume: bool = False,
    initial_steps: int = 0,
    user_reply: str | None = None,
    task_plan: list[dict] | None = None,
) -> dict:
    """运行任务；可选独立目录、事件记录和会话持久化。

    event_sink 会同步接收 Event v1；sink 抛错会立即中止运行。Event 当前只用于
    观察和评测，不参与 session 恢复。
    session_path 不为空时，在开始、每轮工具结果全部写回以及结束状态确定后保存，
    保证文件里的历史始终自洽。resume=True 表示 task 已在 memory 中，不会重复
    写入用户消息；initial_steps 是本次之前已经完成的决策轮数，用于累计步数。
    """
    if type(initial_steps) is not int or initial_steps < 0:
        raise ValueError("initial_steps 必须是非负整数")
    if user_reply is not None and (not resume or not isinstance(user_reply, str) or not user_reply.strip()):
        raise ValueError("user_reply 需要恢复任务且必须是非空字符串")
    if resume:
        last_user = memory.last_user()
        if last_user is not None and not any(
            message.get("role") == "user" and message.get("content") == task
            for message in memory.for_model()
        ) and memory.compactions == 0:
            raise ValueError("恢复的任务与会话中的用户任务不一致")
        # 压缩可能已经把最初的任务移进摘要，这时允许没有用户消息。
        if last_user is None and memory.compactions == 0:
            raise ValueError("恢复失败：记忆中没有用户任务")
    else:
        memory.add_user(task)

    if user_reply is not None:
        memory.add_user(user_reply)
    steps_taken = initial_steps
    plan = validate_task_plan(task_plan or [])
    tool_impl = tools_for_sandbox(sandbox) if sandbox is not None else TOOL_IMPL

    def update_task_plan(items: list) -> str:
        nonlocal plan
        updated = validate_task_plan(items)
        if plan:
            old_goals = {item["goal"] for item in plan}
            new_goals = {item["goal"] for item in updated}
            if not old_goals.issubset(new_goals):
                raise ValueError("已有子任务不能从清单中删除")
        plan = updated
        return json.dumps({"items": plan}, ensure_ascii=False)

    def checked_finish(answer: str) -> str:
        pending = [item["goal"] for item in plan if item["status"] == "pending"]
        if pending:
            raise ValueError("仍有未完成子任务: " + "、".join(pending))
        return tool_impl["finish"](answer=answer)

    registry = ToolRegistry(TOOL_SPECS, {
        **tool_impl, "update_task_plan": update_task_plan, "finish": checked_finish,
    })
    event_emitter = EventEmitter(event_sink)
    model_call_count = 0

    def emit(event_type: str, *, step: int | None = None,
             payload: dict | None = None) -> dict:
        return event_emitter.emit(event_type, step=step, payload=payload)

    def error_data(exc: BaseException) -> dict:
        return {"type": type(exc).__name__, "message": str(exc)}

    def call_model(fn: Callable[[], object], *, purpose: str, step: int,
                   input_metadata: dict) -> object:
        """记录一次模型调用；不在 Event 中重复保存完整输入上下文。"""
        nonlocal model_call_count
        model_call_count += 1
        call_id = f"{event_emitter.run_id}:model:{model_call_count:04d}"
        common = {
            "model_call_id": call_id,
            "purpose": purpose,
            "model": getattr(llm, "model", None),
        }
        emit("model.call.started", step=step,
             payload={**common, "input": input_metadata})
        started = perf_counter()
        try:
            response = fn()
        except BaseException as exc:
            failed_usage = getattr(llm, "last_usage", None)
            if not isinstance(failed_usage, dict):
                failed_usage = None
            emit("model.call.failed", step=step, payload={
                **common,
                "duration_seconds": perf_counter() - started,
                "usage": failed_usage,
                "error": error_data(exc),
            })
            raise
        usage = getattr(llm, "last_usage", None)
        if not isinstance(usage, dict):
            usage = None
        emit("model.call.finished", step=step, payload={
            **common,
            "duration_seconds": perf_counter() - started,
            "usage": usage,
            "response": response,
        })
        return response

    def persist(status: str) -> None:
        if session_path is not None:
            save_session(session_path, memory=memory, task=task, status=status,
                         steps=steps_taken, model=getattr(llm, "model", None), task_plan=plan)

    print(bold(f"\n任务: {task}"))
    print(dim(f"沙箱: {sandbox if sandbox is not None else SANDBOX}"))
    if session_path is not None:
        print(dim(f"会话: {session_path}"))

    def report(status: str) -> None:
        print(dim(f"  [状态] {status}"))
        print(dim(f"  [记忆] {len(memory)} 条消息 / {memory.chars()} 字符"
                  f" / 压缩 {memory.compactions} 次"))

    emit("run.started", payload={
        "task": task,
        "model": getattr(llm, "model", None),
        "max_steps": max_steps,
        "initial_steps": initial_steps,
        "resume": resume,
        "sandbox": str(sandbox if sandbox is not None else SANDBOX),
    })
    persist("running")
    for step in range(1, max_steps + 1):
        event_step = initial_steps + step
        if memory.needs_compaction():
            before_chars = memory.chars()
            print(dim(f"  [记忆] {before_chars} 字符 > 预算 {memory.budget_chars}，压缩最旧的轮次…"))
            emit("memory.compaction.started", step=event_step, payload={
                "before_chars": before_chars,
                "budget_chars": memory.budget_chars,
                "compactions_before": memory.compactions,
            })

            def summarize(transcript: str) -> str:
                return call_model(
                    lambda: llm.summarize(transcript),
                    purpose="compaction",
                    step=event_step,
                    input_metadata={"transcript_chars": len(transcript)},
                )

            compaction_started = perf_counter()
            try:
                compacted = memory.compact(summarize)
            except BaseException as exc:
                emit("memory.compaction.failed", step=event_step, payload={
                    "before_chars": before_chars,
                    "after_chars": memory.chars(),
                    "duration_seconds": perf_counter() - compaction_started,
                    "error": error_data(exc),
                })
                raise
            emit("memory.compaction.finished", step=event_step, payload={
                "compacted": compacted,
                "before_chars": before_chars,
                "after_chars": memory.chars(),
                "duration_seconds": perf_counter() - compaction_started,
                "compactions_after": memory.compactions,
            })
            if compacted:
                print(dim(f"  [记忆] 压缩后 {memory.chars()} 字符"))
            else:
                print(dim("  [记忆] 最近几轮压不动，交给步数预算兜底"))

        print(bold(f"[{step}]"))

        messages = memory.for_model()
        msg = call_model(
            lambda: llm.chat(messages),
            purpose="decision",
            step=event_step,
            input_metadata={
                "message_count": len(messages),
                "message_chars": len(json.dumps(messages, ensure_ascii=False)),
                "tool_count": len(TOOLS),
            },
        )
        steps_taken = event_step
        memory.add_assistant(msg)

        thought = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []

        if thought:
            print(f"  {dim('think:')} {thought}")

        if not calls:
            if any(item["status"] == "pending" for item in plan):
                print(red("\n  ! 子任务清单仍有未完成项，不能认定任务结束"))
                report("incomplete_plan")
                persist("incomplete_plan")
                emit("run.finished", payload={
                    "status": "incomplete_plan", "steps": steps_taken, "answer": thought,
                })
                return {"status": "incomplete_plan", "answer": thought, "steps": steps_taken}
            print(green("\n  = 模型没有再调用工具，结束"))
            report("no_tool_call")
            persist("no_tool_call")
            emit("run.finished", payload={
                "status": "no_tool_call", "steps": steps_taken, "answer": thought,
            })
            return {"status": "no_tool_call", "answer": thought, "steps": steps_taken}

        terminal_answer = None
        terminal_status = None
        for call in calls:
            name = call["function"]["name"]
            tool_call_id = call["id"]

            # finish 之后的调用不执行，但仍需写回结果，保持这批消息配对完整。
            if terminal_status is not None:
                skipped = ToolResult(tool_call_id, name, "skipped", "ERROR: 已调用终止工具，此工具未执行",
                                     error={"code": "skipped_after_terminal", "message": "此工具未执行"})
                observation = skipped.content
                print(f"  {cyan('act:  ')} {name}（跳过）")
                print(f"  {red('obs:  ')} {observation}")
                emit("tool.call.finished", step=event_step, payload={
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "outcome": skipped.outcome,
                    "duration_seconds": None,
                    "content": observation,
                    "raw_arguments": call["function"].get("arguments"),
                    "error": skipped.error,
                })
                memory.add_tool(tool_call_id, observation)
                continue

            raw_args = call["function"].get("arguments")
            if raw_args is None or raw_args == "":
                raw_args = "{}"
            print(f"  {cyan('act:  ')} {name}({raw_args})")

            common = {
                "tool_call_id": tool_call_id,
                "name": name,
            }
            result = registry.execute(
                ToolCall(tool_call_id, name, raw_args),
                on_start=lambda args: emit("tool.call.started", step=event_step,
                                           payload={**common, "arguments": args}),
            )
            observation = result.content
            print(f"  {red('obs:  ') if result.outcome != 'success' else dim('obs:  ')} {observation}")
            terminal_payload = {**common, "outcome": result.outcome,
                                "duration_seconds": result.duration_seconds,
                                "content": observation}
            if result.outcome == "rejected":
                terminal_payload["raw_arguments"] = raw_args
            if result.error is not None:
                terminal_payload["error"] = result.error
            emit("tool.call.finished", step=event_step, payload=terminal_payload)
            memory.add_tool(tool_call_id, observation)
            if result.outcome == "success" and name in {"finish", "ask_user"}:
                terminal_status = "completed" if name == "finish" else "needs_input"
                terminal_answer = observation

        if terminal_status is not None:
            print(green(f"\n  = {terminal_answer}"))
            print(dim(f"\n  共 {steps_taken} 步"))
            report(terminal_status)
            persist(terminal_status)
            emit("run.finished", payload={
                "status": terminal_status, "steps": steps_taken, "answer": terminal_answer,
            })
            return {"status": terminal_status, "answer": terminal_answer, "steps": steps_taken}

        # 这一批工具结果已经全部写回，历史此刻是自洽的，可以存档。
        persist("running")

    print(red(f"\n  ! 达到本次最大步数 {max_steps}，强制停止"))
    report("step_limit")
    persist("step_limit")
    emit("run.finished", payload={
        "status": "step_limit", "steps": steps_taken, "answer": None,
    })
    return {"status": "step_limit", "answer": None, "steps": steps_taken}


# ---------------------------------------------------------------- 入口


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Astrion Agent —— 以智慧，奔赴星海。"
    )
    parser.add_argument("task", nargs="?", default="读取 data.txt，算出所有数字的和，把结果写入 result.txt")
    parser.add_argument("--steps", type=int, default=10, help="本次最多进行的模型决策轮数")
    parser.add_argument("--budget", type=int, default=12000, help="记忆预算（字符），超出后压缩最旧的轮次")
    parser.add_argument("--model", default="", help="临时覆盖 AGENT_MODEL")
    parser.add_argument("--base-url", default="", help="临时覆盖 AGENT_BASE_URL")
    parser.add_argument("--session", type=Path, default=None,
                        help="会话 JSON 路径；存在时恢复记忆，运行中每轮自动保存")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略已有会话文件，从空记忆开始（需要 --session）")
    parser.add_argument("--reply", default=None,
                        help="回答 needs_input 会话提出的问题，并继续原任务")
    args = parser.parse_args()

    if args.steps < 0:
        parser.error("--steps 必须为非负整数")
    if args.budget <= 0:
        parser.error("--budget 必须为正整数")
    if args.fresh and args.session is None:
        parser.error("--fresh 需要同时指定 --session")
    if args.reply is not None and (args.session is None or args.fresh or not args.reply.strip()):
        parser.error("--reply 需要已有会话、非空回答，且不能与 --fresh 同用")

    # 先加载 .env，再用统一的方式读配置
    env_files = load_env(find_env_files(Path(__file__).resolve().parent))

    key = first_nonempty(
        os.environ.get("AGENT_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    base_url = args.base_url or os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com"
    model = args.model or os.environ.get("AGENT_MODEL") or "deepseek-chat"

    if not key:
        hint = env_files[0] if env_files else Path(__file__).resolve().parent.parent / ".env"
        print(red("错误: 没有可用的 API key，无法启动。"), file=sys.stderr)
        print(dim(f"      把 key 填到这个文件的 AGENT_API_KEY= 后面: {hint}"), file=sys.stderr)
        raise SystemExit(1)

    source = f"来自 {env_files[0]}" if env_files else "来自 shell 环境变量"
    print(dim(f"模型: {model} @ {base_url}"))
    print(dim(f"密钥: {mask(key)}（{source}）"))
    llm = ChatLLM(key=key, base_url=base_url, model=model)

    session_path = args.session.expanduser() if args.session is not None else None
    session = None
    if session_path is not None and not args.fresh and session_path.exists():
        try:
            session = load_session(session_path, system_prompt=SYSTEM_PROMPT,
                                   budget_chars=args.budget)
        except (OSError, ValueError) as exc:
            print(red(f"错误: 无法读取会话文件 {session_path}: {exc}"), file=sys.stderr)
            print(dim("      可以用 --fresh 忽略旧文件并重新开始。"), file=sys.stderr)
            raise SystemExit(1)

    if session is not None:
        memory = session.memory
        if args.reply is not None:
            if session.status != "needs_input":
                parser.error("--reply 只能用于等待用户回答的会话")
            if args.task != parser.get_default("task") and args.task != session.task:
                parser.error("--reply 的任务必须与会话中的原任务一致")
            args.task = session.task
        if session.status == "needs_input" and args.reply is None:
            parser.error("会话等待用户回答；请使用 --reply 提供补充信息")
        resume = session.is_resumable(args.task)
        initial_steps = session.steps if resume else 0
        if resume:
            print(dim(f"会话: 恢复 {session_path}（{len(memory)} 条消息，已累计 {session.steps} 步）"))
        else:
            print(dim(f"会话: 使用 {session_path} 的已有记忆开始新任务"))
            if session.task != args.task:
                print(dim(f"      上次任务: {session.task}"))
    else:
        if args.reply is not None:
            parser.error("--reply 需要一个已存在的 needs_input 会话")
        memory = Memory(SYSTEM_PROMPT, budget_chars=args.budget)
        resume = False
        initial_steps = 0
        if args.fresh and session_path is not None and session_path.exists():
            print(dim(f"会话: 忽略已有文件 {session_path}，从空记忆开始"))

    try:
        run(args.task, llm, memory, max_steps=args.steps, session_path=session_path,
            resume=resume, initial_steps=initial_steps, user_reply=args.reply,
            task_plan=list(session.task_plan) if session is not None and resume else None)
    except OSError as exc:
        print(red(f"错误: 会话文件写入失败: {exc}"), file=sys.stderr)
        raise SystemExit(1)
    except ValueError as exc:
        print(red(f"错误: {exc}"), file=sys.stderr)
        raise SystemExit(1)
    if session_path is not None:
        print(dim(f"会话已保存: {session_path}"))


if __name__ == "__main__":
    main()
