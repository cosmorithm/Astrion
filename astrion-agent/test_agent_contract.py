"""默认 agent 行为的契约测试：新增的工具注入不能改变旧路径。"""

import copy
import inspect
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import agent
from memory import Memory


class ScriptedLLM:
    model = "scripted"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def chat(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        return copy.deepcopy(next(self.replies))


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def chat_payload(self, tools_marker="__default__"):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }).encode("utf-8"))

        llm = agent.ChatLLM(key="test-key", base_url="https://example.test/v1",
                            model="test-model")
        with patch("agent.urllib.request.urlopen", side_effect=fake_urlopen):
            if tools_marker == "__default__":
                llm.chat([{"role": "user", "content": "hello"}])
            else:
                llm.chat([{"role": "user", "content": "hello"}], tools=tools_marker)
        return json.loads(requests[0].data.decode("utf-8"))

    def test_default_chat_still_sends_the_default_tools(self):
        payload = self.chat_payload()

        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(payload["tools"], agent.TOOLS)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["temperature"], 0)

    def test_chat_accepts_a_case_specific_tool_list(self):
        custom = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

        payload = self.chat_payload(tools_marker=custom)

        self.assertEqual(payload["tools"], custom)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertNotIn("finish", [tool["function"]["name"] for tool in payload["tools"]])

    def test_empty_tool_list_omits_tools_and_tool_choice(self):
        payload = self.chat_payload(tools_marker=[])

        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["temperature"], 0)

    def test_chat_rejects_a_non_list_tool_argument(self):
        llm = agent.ChatLLM(key="test-key", base_url="https://example.test/v1",
                            model="test-model")

        with self.assertRaises(TypeError):
            llm.chat([{"role": "user", "content": "hello"}], tools="not-a-list")

    def test_default_run_keeps_the_old_prompt_and_does_not_save_a_session(self):
        llm = ScriptedLLM([{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {"name": "finish", "arguments": '{"answer":"完成"}'},
            }],
        }])

        with patch.object(agent, "save_session") as save_mock:
            result = agent.run("测试任务", llm, Memory("契约系统提示"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(llm.inputs[0][0],
                         {"role": "system", "content": "契约系统提示"})
        self.assertEqual(agent.TOOLS[-1]["function"]["name"], "finish")
        save_mock.assert_not_called()

    def test_default_run_signature_keeps_optional_persistence_off(self):
        parameters = inspect.signature(agent.run).parameters

        self.assertIsNone(parameters["session_path"].default)
        self.assertFalse(parameters["resume"].default)
        self.assertEqual(parameters["initial_steps"].default, 0)
        self.assertIsNone(parameters["sandbox"].default)


if __name__ == "__main__":
    unittest.main()
