#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toy-agent —— 一个可以完整看懂的最小智能体（零依赖，标准库实现）

它只做一件事：把「模型 + 工具 + 循环」跑通，并把每一步打印出来，
让你能亲眼看见 ReAct 的三段式：think → act → obs。

用法:
    python3 agent.py "看看 data.txt 里数字的和是多少"   # 调真模型

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
import copy
import json
import operator as _op
import os
import sys
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from typing import Callable

from memory import Memory

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

    这样无论 .env 放在 toy-agent/ 还是上一层的 Multi Agent/，都能被找到。
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
}


def tools_for_sandbox(sandbox: Path) -> dict:
    """为一次运行绑定独立文件目录，不改变全局 SANDBOX。"""
    return {
        **TOOL_IMPL,
        "list_files": partial(TOOL_IMPL["list_files"], sandbox=sandbox),
        "read_file": partial(TOOL_IMPL["read_file"], sandbox=sandbox),
        "write_file": partial(TOOL_IMPL["write_file"], sandbox=sandbox),
    }

SYSTEM_PROMPT = """你是一个最小智能体，只能通过工具来观察和改变 sandbox/ 目录。

规则：
1. 一次只做一个动作，等观察结果回来再决定下一步。
2. 不要凭空猜测文件内容，先读。
3. 任务完成时调用 finish 工具。
4. 用中文思考和回答。"""

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

    def _record_usage(self, usage: dict | None, purpose: str) -> None:
        """记录一次响应的用量。缺失或不完整时标记未知，不按零计算。"""
        self.usage_calls += 1
        valid = isinstance(usage, dict) and all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in self.usage_totals
        )
        if valid:
            for key in self.usage_totals:
                self.usage_totals[key] += usage[key]
            print(dim(f"  [token][{purpose}] 本次：输入 {usage['prompt_tokens']}"
                      f" / 输出 {usage['completion_tokens']} / 合计 {usage['total_tokens']}"))
        else:
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

    def chat(self, messages: list) -> dict:
        return self._post({
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0,
        })

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
) -> dict:
    """运行任务；可选独立目录和事件记录，不影响默认调用方式。"""
    memory.add_user(task)
    tool_impl = tools_for_sandbox(sandbox) if sandbox is not None else TOOL_IMPL

    def emit(event: dict) -> None:
        # 独立副本避免后续消息变化改写已经记录的事件。
        if event_sink is not None:
            event_sink(copy.deepcopy(event))

    print(bold(f"\n任务: {task}"))
    print(dim(f"沙箱: {sandbox if sandbox is not None else SANDBOX}"))

    def report(status: str) -> None:
        print(dim(f"  [状态] {status}"))
        print(dim(f"  [记忆] {len(memory)} 条消息 / {memory.chars()} 字符"
                  f" / 压缩 {memory.compactions} 次"))

    steps_taken = 0
    for step in range(1, max_steps + 1):
        if memory.needs_compaction():
            before_chars = memory.chars()
            print(dim(f"  [记忆] {before_chars} 字符 > 预算 {memory.budget_chars}，压缩最旧的轮次…"))

            def summarize(transcript: str) -> str:
                emit({"type": "summary_request", "step": step, "messages": [
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": transcript},
                ]})
                summary = llm.summarize(transcript)
                emit({"type": "summary_response", "step": step, "content": summary})
                return summary

            compacted = memory.compact(summarize)
            emit({"type": "memory_compaction", "step": step, "compacted": compacted,
                  "before_chars": before_chars, "after_chars": memory.chars()})
            if compacted:
                print(dim(f"  [记忆] 压缩后 {memory.chars()} 字符"))
            else:
                print(dim("  [记忆] 最近几轮压不动，交给步数预算兜底"))

        print(bold(f"[{step}]"))

        messages = memory.for_model()
        emit({"type": "model_request", "step": step, "messages": messages})
        msg = llm.chat(messages)
        steps_taken = step
        memory.add_assistant(msg)
        emit({"type": "model_response", "step": step, "message": memory.for_model()[-1]})

        thought = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []

        if thought:
            print(f"  {dim('think:')} {thought}")

        if not calls:
            print(green("\n  = 模型没有再调用工具，结束"))
            report("no_tool_call")
            emit({"type": "run_end", "status": "no_tool_call", "steps": steps_taken})
            return {"status": "no_tool_call", "answer": thought, "steps": steps_taken}

        finish_answer = None
        for call in calls:
            name = call["function"]["name"]

            # finish 之后的调用不执行，但仍需写回结果，保持这批消息配对完整。
            if finish_answer is not None:
                observation = "ERROR: 已调用 finish，此工具未执行"
                print(f"  {cyan('act:  ')} {name}（跳过）")
                print(f"  {red('obs:  ')} {observation}")
                memory.add_tool(call["id"], observation)
                emit({"type": "tool_result", "step": step, "name": name,
                      "tool_call_id": call["id"], "outcome": "skipped", "content": observation})
                continue

            raw_args = call["function"].get("arguments")
            if raw_args is None or raw_args == "":
                raw_args = "{}"
            print(f"  {cyan('act:  ')} {name}({raw_args})")
            outcome = "ok"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    raise TypeError("工具参数必须是 JSON 对象")
                if name == "finish":
                    answer = args.get("answer")
                    if not isinstance(answer, str) or not answer.strip():
                        raise ValueError("finish 的 answer 必须是非空字符串")
                observation = tool_impl[name](**args)
                if name == "finish":
                    finish_answer = answer
                print(f"  {dim('obs:  ')} {observation}")
            except Exception as e:
                outcome = "error"
                observation = f"ERROR: {type(e).__name__}: {e}"
                print(f"  {red('obs:  ')} {observation}")

            memory.add_tool(call["id"], str(observation))
            emit({"type": "tool_result", "step": step, "name": name,
                  "tool_call_id": call["id"], "outcome": outcome, "content": str(observation)})

        if finish_answer is not None:
            print(green(f"\n  = {finish_answer}"))
            print(dim(f"\n  共 {steps_taken} 步"))
            report("completed")
            emit({"type": "run_end", "status": "completed", "steps": steps_taken})
            return {"status": "completed", "answer": finish_answer, "steps": steps_taken}

    print(red(f"\n  ! 达到最大步数 {max_steps}，强制停止"))
    report("step_limit")
    emit({"type": "run_end", "status": "step_limit", "steps": steps_taken})
    return {"status": "step_limit", "answer": None, "steps": steps_taken}


# ---------------------------------------------------------------- 入口


def main() -> None:
    parser = argparse.ArgumentParser(description="一个最小可读的智能体")
    parser.add_argument("task", nargs="?", default="读取 data.txt，算出所有数字的和，把结果写入 result.txt")
    parser.add_argument("--steps", type=int, default=10, help="最大步数")
    parser.add_argument("--budget", type=int, default=12000, help="记忆预算（字符），超出后压缩最旧的轮次")
    parser.add_argument("--model", default="", help="临时覆盖 AGENT_MODEL")
    parser.add_argument("--base-url", default="", help="临时覆盖 AGENT_BASE_URL")
    args = parser.parse_args()

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

    memory = Memory(SYSTEM_PROMPT, budget_chars=args.budget)
    run(args.task, llm, memory, max_steps=args.steps)


if __name__ == "__main__":
    main()
