"""基础文件、算术与控制工具；agent.py 保留兼容导出。"""

from __future__ import annotations
import ast
import json
import operator as _op
from pathlib import Path
from functools import partial
from tooling import ToolSpec
from context_tools import CONTEXT_SPECS

# ---------------------------------------------------------------- 沙箱

SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"


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
    return json.dumps(
        {"ok": True, "path": path, "bytes": len(content.encode())}, ensure_ascii=False
    )


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
                "properties": {
                    "path": {"type": "string", "description": "相对 sandbox 的路径，默认 '.'"}
                },
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
TOOLS.insert(
    -1,
    {
        "type": "function",
        "function": {
            "name": "update_task_plan",
            "description": "为多目标任务记录完整子任务清单并更新状态；完成状态必须有工具结果或其他事实依据",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string", "minLength": 1},
                                "status": {"type": "string", "enum": ["pending", "completed"]},
                            },
                            "required": ["goal", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    },
)
TOOLS.insert(
    -1,
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "缺少必要信息时暂停任务并向用户询问",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
)

_TOOL_OUTPUTS = {
    "list_files": "目录路径和文件列表",
    "read_file": "文件路径和文本内容（可能截断）",
    "write_file": "写入是否成功、文件路径和字节数",
    "calculator": "算术表达式和计算结果",
    "finish": "提交给用户的最终回答",
    "ask_user": "等待用户回答的问题",
    "update_task_plan": "本次任务的子目标及其声明状态",
}
for _tool in TOOLS:
    _tool["function"]["parameters"].setdefault("additionalProperties", False)
TOOL_SPECS = [
    ToolSpec(
        name=tool["function"]["name"],
        description=tool["function"]["description"],
        input_schema=tool["function"]["parameters"],
        output_description=_TOOL_OUTPUTS[tool["function"]["name"]],
        terminal=tool["function"]["name"] in {"finish", "ask_user"},
    )
    for tool in TOOLS
]
TOOL_SPECS[-1:-1] = CONTEXT_SPECS
TOOLS = [spec.as_api_tool() for spec in TOOL_SPECS]


def tools_for_sandbox(sandbox: Path) -> dict:
    """为一次运行绑定独立文件目录，不改变全局 SANDBOX。"""
    return {
        **TOOL_IMPL,
        "list_files": partial(TOOL_IMPL["list_files"], sandbox=sandbox),
        "read_file": partial(TOOL_IMPL["read_file"], sandbox=sandbox),
        "write_file": partial(TOOL_IMPL["write_file"], sandbox=sandbox),
    }
