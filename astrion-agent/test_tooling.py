"""工具契约及 Agent 暂停、子任务清单的离线回归测试。"""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent
from events import EventRecorder
from memory import Memory
from session import load_session
from tooling import ToolCall, ToolRegistry, ToolSpec


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}


def message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def chat(self, messages):
        self.messages.append(copy.deepcopy(messages))
        return next(self.responses)


class ToolingTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        def handler(path):
            self.seen.append(path)
            return "ok"
        self.spec = ToolSpec("read", "读取", {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        })
        self.registry = ToolRegistry([self.spec], {"read": handler})

    def test_invalid_calls_never_reach_handler(self):
        cases = [
            ("other", '{}', "unknown_tool"),
            ("read", '{', "invalid_json"),
            ("read", '[]', "invalid_argument_type"),
            ("read", '{}', "missing_required_argument"),
            ("read", '{"path": 3}', "invalid_argument_type"),
            ("read", '{"path": "a", "extra": 1}', "unexpected_argument"),
            ("read", '{"path": NaN}', "invalid_json"),
        ]
        for name, raw, code in cases:
            with self.subTest(code=code):
                starts = []
                result = self.registry.execute(ToolCall("1", name, raw), starts.append)
                self.assertEqual(result.outcome, "rejected")
                self.assertEqual(result.error["code"], code)
                self.assertEqual(starts, [])
        self.assertEqual(self.seen, [])

    def test_success_and_execution_error_are_distinct(self):
        starts = []
        result = self.registry.execute(ToolCall("1", "read", '{"path":"a"}'), starts.append)
        self.assertEqual((result.outcome, result.content), ("success", "ok"))
        self.assertEqual(starts, [{"path": "a"}])
        def failing(path):
            raise FileNotFoundError(path)
        failed = ToolRegistry([self.spec], {"read": failing}).execute(ToolCall("2", "read", '{"path":"x"}'))
        self.assertEqual(failed.outcome, "error")
        self.assertEqual(failed.error["code"], "execution_failed")
        self.assertIsNotNone(failed.duration_seconds)

    def test_schema_and_implementation_mismatch_fails_on_registration(self):
        with self.assertRaisesRegex(ValueError, "无法绑定"):
            ToolRegistry([self.spec], {"read": lambda wrong: None})
        bad = ToolSpec("bad", "test", {"type": "object", "properties": {
            "x": {"type": "string", "pattern": "x"}}})
        with self.assertRaisesRegex(ValueError, "未支持"):
            ToolRegistry([bad], {"bad": lambda x=None: None})

    def test_number_boolean_and_nested_array_validation(self):
        spec = ToolSpec("many", "test", {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
        }, "required": ["items"], "additionalProperties": False})
        registry = ToolRegistry([spec], {"many": lambda items: "ok"})
        for raw in ('{"items":[true]}', '{"items":[4.0]}'):
            self.assertEqual(registry.execute(ToolCall("1", "many", raw)).error["code"], "invalid_argument_type")

    def test_needs_input_can_resume_with_user_answer_and_plan(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            path = Path(temp) / "session.json"
            plan = [{"goal": "读取文件", "status": "pending"}]
            first = ScriptedLLM([message(call("plan", "update_task_plan", {"items": plan})),
                                 message(call("ask", "ask_user", {"question": "文件名是什么？"}))])
            memory = Memory("规则")
            result = agent.run("读取文件", first, memory, session_path=path)
            self.assertEqual(result["status"], "needs_input")
            saved = load_session(path)
            self.assertTrue(saved.is_resumable("读取文件"))
            self.assertEqual(list(saved.task_plan), plan)
            second = ScriptedLLM([
                message(call("plan2", "update_task_plan", {"items": [{"goal": "读取文件", "status": "completed"}]})),
                message(call("finish", "finish", {"answer": "完成"})),
            ])
            result = agent.run("读取文件", second, saved.memory, resume=True,
                               initial_steps=saved.steps, user_reply="data.txt",
                               task_plan=list(saved.task_plan), session_path=path)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(second.messages[0][-1], {"role": "user", "content": "data.txt"})
            self.assertEqual(saved.memory.check(), [])
            self.assertEqual(load_session(path).status, "completed")

    def test_unfinished_plan_blocks_finish_and_keeps_tool_results_paired(self):
        with redirect_stdout(io.StringIO()):
            llm = ScriptedLLM([
                message(call("plan", "update_task_plan", {"items": [
                    {"goal": "任务一", "status": "completed"},
                    {"goal": "任务二", "status": "pending"},
                ]})),
                message(call("early", "finish", {"answer": "完成"})),
                message(call("plan2", "update_task_plan", {"items": [
                    {"goal": "任务一", "status": "completed"},
                    {"goal": "任务二", "status": "completed"},
                ]})),
                message(call("done", "finish", {"answer": "完成"})),
            ])
            recorder = EventRecorder()
            memory = Memory("规则")
            result = agent.run("两个任务", llm, memory, event_sink=recorder)
            self.assertEqual(result["status"], "completed")
            self.assertIn("仍有未完成子任务", llm.messages[2][-1]["content"])
            self.assertEqual(memory.check(), [])
            early = [e for e in recorder.events if e["payload"].get("tool_call_id") == "early"]
            self.assertEqual(early[-1]["payload"]["outcome"], "error")

    def test_tools_after_ask_user_are_skipped(self):
        with redirect_stdout(io.StringIO()):
            llm = ScriptedLLM([message(call("ask", "ask_user", {"question": "文件名？"}),
                                        call("calc", "calculator", {"expression": "1+1"}))])
            memory = Memory("规则")
            result = agent.run("问题", llm, memory)
            self.assertEqual(result["status"], "needs_input")
            self.assertIn("此工具未执行", memory.for_model()[-1]["content"])
            self.assertEqual(memory.check(), [])

    def test_no_tool_response_with_pending_tasks_is_resumable(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            path = Path(temp) / "session.json"
            llm = ScriptedLLM([
                message(call("plan", "update_task_plan", {"items": [
                    {"goal": "查询股票", "status": "pending"},
                ]})),
                {"role": "assistant", "content": "先到这里"},
            ])
            result = agent.run("查询股票", llm, Memory("规则"), session_path=path)
            self.assertEqual(result["status"], "incomplete_plan")
            saved = load_session(path)
            self.assertTrue(saved.is_resumable("查询股票"))
            self.assertEqual(saved.task_plan[0]["status"], "pending")

    def test_cli_reply_continues_original_session(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            path = Path(temp) / "session.json"
            first = ScriptedLLM([message(call("ask", "ask_user", {"question": "文件名？"}))])
            agent.run("读取文件", first, Memory("规则"), session_path=path)
            second = ScriptedLLM([message(call("finish", "finish", {"answer": "收到"}))])
            with patch.object(sys, "argv", ["agent.py", "--session", str(path), "--reply", "data.txt"]), \
                 patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
                 patch.object(agent, "find_env_files", return_value=[]), \
                 patch.object(agent, "ChatLLM", return_value=second):
                agent.main()
            self.assertEqual(second.messages[0][-1], {"role": "user", "content": "data.txt"})
            saved = load_session(path)
            self.assertEqual(saved.task, "读取文件")
            self.assertEqual(saved.status, "completed")

    def test_full_agent_executes_real_sandbox_tools_with_plan(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            (root / "data.txt").write_text("2,3", encoding="utf-8")
            plan = [{"goal": "读取", "status": "pending"},
                    {"goal": "计算并写入", "status": "pending"}]
            llm = ScriptedLLM([
                message(call("plan", "update_task_plan", {"items": plan})),
                message(call("read", "read_file", {"path": "data.txt"})),
                message(call("calc", "calculator", {"expression": "2+3"})),
                message(call("write", "write_file", {"path": "result.txt", "content": "5"})),
                message(call("plan2", "update_task_plan", {"items": [
                    {"goal": "读取", "status": "completed"},
                    {"goal": "计算并写入", "status": "completed"},
                ]})),
                message(call("finish", "finish", {"answer": "已写入"})),
            ])
            memory = Memory("规则")
            result = agent.run("求和", llm, memory, sandbox=root)
            self.assertEqual(result["status"], "completed")
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "5")
            self.assertEqual(json.loads(llm.messages[2][-1]["content"])["content"], "2,3")
            self.assertEqual(memory.check(), [])


if __name__ == "__main__":
    unittest.main()
