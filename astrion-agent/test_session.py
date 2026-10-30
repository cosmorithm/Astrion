"""会话持久化的离线测试：不访问网络，也不读写真实 sandbox。"""

import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent
from memory import Memory
from session import (
    RESUMABLE_STATUSES,
    SESSION_VERSION,
    load_session,
    save_session,
)


def tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def tool_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ScriptedLLM:
    model = "scripted-test-model"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def chat(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)


class FakeLLM:
    model = "fake-model"


class SessionTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="astrion-agent-session-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        self.path = self.root / "session.json"
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def make_memory(self):
        memory = Memory("系统规则", budget_chars=4000, keep_recent=2)
        memory.add_user("测试任务")
        memory.add_assistant(
            tool_message(tool_call("calc-1", "calculator", '{"expression":"1+1"}'))
        )
        memory.add_tool("calc-1", '{"expression":"1+1","result":2}')
        return memory

    def invoke_main(self, argv):
        with patch.object(agent, "load_env", return_value=[]), \
                patch.object(agent, "find_env_files", return_value=[]), \
                patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}, clear=False), \
                patch.object(agent, "ChatLLM", return_value=FakeLLM()), \
                patch.object(agent, "run", return_value={
                    "status": "completed", "answer": "", "steps": 0,
                }) as run_mock, \
                patch.object(sys, "argv", ["agent.py", *argv]):
            agent.main()
        return run_mock

    # ------------------------------------------------------------ Memory 序列化

    def test_memory_dict_round_trip_preserves_history_summary_and_state(self):
        memory = self.make_memory()
        memory._summary = "之前算过 1+1"
        memory.compactions = 1

        data = memory.to_dict()
        restored = Memory.from_dict(copy.deepcopy(data))

        self.assertEqual(restored.to_dict(), data)
        self.assertEqual(restored.check(), [])
        self.assertEqual(len(restored), 3)
        self.assertEqual(restored.last_user(), "测试任务")
        self.assertEqual(restored.chars(), memory.chars())

    def test_to_dict_returns_an_independent_copy(self):
        memory = self.make_memory()

        data = memory.to_dict()
        data["history"][0]["content"] = "被外部改掉"
        data["history"].append({"role": "user", "content": "多出来的一条"})

        self.assertEqual(memory.last_user(), "测试任务")
        self.assertEqual(len(memory), 3)

    def test_from_dict_rejects_inconsistent_history(self):
        data = self.make_memory().to_dict()
        data["history"].append(
            {"role": "tool", "tool_call_id": "missing", "content": "没有对应调用"}
        )

        with self.assertRaises(ValueError):
            Memory.from_dict(data)

    def test_memory_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            Memory("规则", budget_chars=0)
        with self.assertRaises(ValueError):
            Memory("规则", keep_recent=0)

    # ------------------------------------------------------------ 文件保存与加载

    def test_save_and_load_preserves_metadata_and_memory(self):
        memory = self.make_memory()

        saved = save_session(self.path, memory=memory, task="测试任务",
                             status="running", steps=2, model="test-model")

        self.assertEqual(saved, self.path.resolve())
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], SESSION_VERSION)
        self.assertEqual(raw["task"], "测试任务")
        self.assertEqual(raw["status"], "running")
        self.assertEqual(raw["steps"], 2)
        self.assertEqual(raw["model"], "test-model")
        self.assertEqual(raw["memory"], memory.to_dict())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

        loaded = load_session(self.path)
        self.assertEqual(loaded.path, self.path.resolve())
        self.assertEqual(loaded.task, "测试任务")
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.steps, 2)
        self.assertEqual(loaded.model, "test-model")
        self.assertEqual(loaded.memory.to_dict(), memory.to_dict())
        self.assertEqual(loaded.memory.check(), [])

    def test_load_can_override_runtime_configuration(self):
        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=0)

        loaded = load_session(self.path, system_prompt="新的系统规则",
                              budget_chars=999, keep_recent=1)

        self.assertEqual(loaded.memory.system_prompt, "新的系统规则")
        self.assertEqual(loaded.memory.budget_chars, 999)
        self.assertEqual(loaded.memory.keep_recent, 1)

    def test_legacy_v1_session_without_plan_loads_as_empty(self):
        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.pop("task_plan")
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        restored = load_session(self.path)
        self.assertEqual(restored.task_plan, ())
        self.assertTrue(restored.is_resumable("测试任务"))

    def test_save_keeps_the_old_file_when_replace_fails(self):
        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=1)
        original = self.path.read_text(encoding="utf-8")

        with patch("session.os.replace", side_effect=OSError("磁盘错误")):
            with self.assertRaises(OSError):
                save_session(self.path, memory=self.make_memory(), task="测试任务",
                             status="completed", steps=2)

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual([item.name for item in self.root.iterdir()], ["session.json"])

    def test_save_refuses_an_inconsistent_memory(self):
        memory = Memory("系统规则")
        memory.add_tool("dangling", "没有对应调用")

        with self.assertRaises(ValueError):
            save_session(self.path, memory=memory, task="测试任务",
                         status="running", steps=0)

        self.assertFalse(self.path.exists())

    def test_load_rejects_invalid_json_and_unknown_version(self):
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_session(self.path)

        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["version"] = 999
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_session(self.path)

    # ------------------------------------------------------------ 主循环保存与恢复

    def test_completed_run_saves_a_finished_session(self):
        llm = ScriptedLLM([
            tool_message(tool_call("calc-1", "calculator", '{"expression":"1+1"}')),
            tool_message(tool_call("finish-1", "finish", '{"answer":"完成"}')),
        ])
        memory = Memory("系统规则")

        result = agent.run("测试任务", llm, memory, session_path=self.path)

        self.assertEqual(result, {"status": "completed", "answer": "完成", "steps": 2})
        session = load_session(self.path)
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.steps, 2)
        self.assertEqual(session.task, "测试任务")
        self.assertFalse(session.is_resumable("测试任务"))
        self.assertEqual(session.memory.check(), [])
        self.assertEqual(session.memory.last_user(), "测试任务")

    def test_resume_after_crash_does_not_duplicate_the_user_task(self):
        crashed_llm = ScriptedLLM([
            tool_message(tool_call("calc-1", "calculator", '{"expression":"1+1"}')),
            RuntimeError("模拟中断"),
        ])
        memory = Memory("系统规则")

        with self.assertRaises(RuntimeError):
            agent.run("测试任务", crashed_llm, memory, session_path=self.path)

        crashed = load_session(self.path)
        self.assertEqual(crashed.status, "running")
        self.assertEqual(crashed.steps, 1)
        self.assertTrue(crashed.is_resumable("测试任务"))

        resumed_llm = ScriptedLLM([
            tool_message(tool_call("finish-1", "finish", '{"answer":"恢复完成"}')),
        ])
        result = agent.run("测试任务", resumed_llm, crashed.memory,
                           session_path=self.path, resume=True,
                           initial_steps=crashed.steps)

        self.assertEqual(
            result,
            {"status": "completed", "answer": "恢复完成", "steps": 2},
        )
        self.assertEqual(
            sum(message["role"] == "user" for message in resumed_llm.inputs[0]),
            1,
        )
        done = load_session(self.path)
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.steps, 2)

    def test_step_limit_session_can_continue(self):
        first_llm = ScriptedLLM([
            tool_message(tool_call("calc-1", "calculator", '{"expression":"1+1"}')),
        ])
        memory = Memory("系统规则")

        first = agent.run("测试任务", first_llm, memory, max_steps=1,
                          session_path=self.path)

        self.assertEqual(first["status"], "step_limit")
        session = load_session(self.path)
        self.assertEqual(session.status, "step_limit")
        self.assertEqual(session.steps, 1)
        self.assertIn("step_limit", RESUMABLE_STATUSES)
        self.assertTrue(session.is_resumable("测试任务"))
        self.assertFalse(session.is_resumable("另一个任务"))

        second_llm = ScriptedLLM([
            tool_message(tool_call("finish-1", "finish", '{"answer":"继续完成"}')),
        ])
        second = agent.run("测试任务", second_llm, session.memory, max_steps=10,
                           session_path=self.path, resume=True,
                           initial_steps=session.steps)

        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["steps"], 2)

    def test_resume_after_compaction_does_not_need_a_user_message(self):
        memory = Memory("系统规则", budget_chars=4000, keep_recent=1)
        memory.add_user("测试任务")
        memory.add_assistant(
            tool_message(tool_call("calc-1", "calculator", '{"expression":"1+1"}'))
        )
        memory.add_tool("calc-1", '{"expression":"1+1","result":2}')

        self.assertTrue(memory.compact(lambda transcript: "之前执行过计算"))
        self.assertIsNone(memory.last_user())
        self.assertEqual(memory.compactions, 1)
        save_session(self.path, memory=memory, task="测试任务",
                     status="running", steps=1)

        loaded = load_session(self.path)
        self.assertIsNone(loaded.memory.last_user())
        resumed_llm = ScriptedLLM([
            tool_message(tool_call("finish-1", "finish", '{"answer":"压缩后恢复"}')),
        ])
        result = agent.run("测试任务", resumed_llm, loaded.memory,
                           session_path=self.path, resume=True,
                           initial_steps=loaded.steps)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"], 2)
        # 原任务已进入摘要，恢复时不能再补一条重复的用户消息。
        self.assertEqual(
            sum(message["role"] == "user" for message in resumed_llm.inputs[0]),
            0,
        )

    def test_no_tool_call_is_not_marked_resumable(self):
        llm = ScriptedLLM([{"role": "assistant", "content": "直接回答"}])

        result = agent.run("测试任务", llm, Memory("系统规则"),
                           session_path=self.path)

        self.assertEqual(result["status"], "no_tool_call")
        session = load_session(self.path)
        self.assertEqual(session.status, "no_tool_call")
        self.assertFalse(session.is_resumable("测试任务"))

    # ------------------------------------------------------------ 命令行行为

    def test_cli_creates_a_new_session(self):
        run_mock = self.invoke_main(["测试任务", "--session", str(self.path)])

        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["session_path"], self.path)
        self.assertFalse(kwargs["resume"])
        self.assertEqual(kwargs["initial_steps"], 0)

    def test_cli_resumes_a_running_task(self):
        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=3)

        run_mock = self.invoke_main(["测试任务", "--session", str(self.path)])

        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["session_path"], self.path)
        self.assertTrue(kwargs["resume"])
        self.assertEqual(kwargs["initial_steps"], 3)

    def test_cli_fresh_ignores_an_existing_session(self):
        save_session(self.path, memory=self.make_memory(), task="测试任务",
                     status="running", steps=3)

        run_mock = self.invoke_main(
            ["测试任务", "--session", str(self.path), "--fresh"]
        )

        kwargs = run_mock.call_args.kwargs
        self.assertFalse(kwargs["resume"])
        self.assertEqual(kwargs["initial_steps"], 0)

    def test_cli_reports_a_corrupt_session(self):
        self.path.write_text("{", encoding="utf-8")
        stderr = io.StringIO()

        with patch.object(agent, "load_env", return_value=[]), \
                patch.object(agent, "find_env_files", return_value=[]), \
                patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}, clear=False), \
                patch.object(agent, "ChatLLM", return_value=FakeLLM()), \
                patch.object(sys, "argv", ["agent.py", "测试任务",
                                           "--session", str(self.path)]), \
                redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                agent.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("无法读取会话文件", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
