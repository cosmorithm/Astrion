"""用模拟 HTTP 响应验证 token 计量，不访问网络。"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent import ChatLLM


def response(message, **fields):
    data = {"choices": [{"message": message}], **fields}
    return io.BytesIO(json.dumps(data).encode("utf-8"))


def usage(prompt, completion):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.llm = ChatLLM(key="test-key", base_url="https://example.invalid", model="test")
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_each_response_is_reported_and_accumulated(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression":"2+3"}'},
            }]},
            {"role": "assistant", "content": "结果是 5"},
        ]
        replies = [response(messages[0], usage=usage(800, 50)),
                   response(messages[1], usage=usage(1100, 80))]
        with patch("agent.urllib.request.urlopen", side_effect=replies):
            self.assertEqual(self.llm.chat([]), messages[0])
            self.assertEqual(self.llm.chat([]), messages[1])

        self.assertEqual(self.llm.usage_totals, usage(1900, 130))
        self.assertEqual(self.llm.usage_calls, 2)
        self.assertEqual(self.llm.usage_unknown, 0)
        log = self.output.getvalue()
        self.assertIn("[token][决策] 本次：输入 800 / 输出 50 / 合计 850", log)
        self.assertIn("[token][决策] 本次：输入 1100 / 输出 80 / 合计 1180", log)
        self.assertIn("累计：输入 1900 / 输出 130 / 合计 2030", log)

    def test_summary_call_is_included_in_the_same_total(self):
        replies = [
            response({"role": "assistant", "content": "决策"}, usage=usage(800, 50)),
            response({"role": "assistant", "content": "  已读取文件  "}, usage=usage(300, 40)),
        ]
        with patch("agent.urllib.request.urlopen", side_effect=replies) as send:
            self.llm.chat([])
            self.assertEqual(self.llm.summarize("旧执行记录"), "已读取文件")

        summary_request = json.loads(send.call_args.args[0].data)
        self.assertNotIn("tools", summary_request)
        self.assertEqual(self.llm.usage_totals, usage(1100, 90))
        self.assertEqual(self.llm.usage_calls, 2)
        self.assertIn("[token][记忆摘要] 本次：输入 300 / 输出 40 / 合计 340",
                      self.output.getvalue())

    def test_absent_usage_is_unknown_and_does_not_block_the_answer(self):
        message = {"role": "assistant", "content": "仍可正常回答"}
        with patch("agent.urllib.request.urlopen", return_value=response(message)):
            self.assertEqual(self.llm.chat([]), message)

        self.assertEqual(self.llm.usage_unknown, 1)
        self.assertIn("本次：未知", self.output.getvalue())
        self.assertIn("累计：未知（1 次调用用量未知）", self.output.getvalue())
        self.assertNotIn("累计：输入 0", self.output.getvalue())

    def test_invalid_or_partial_usage_is_excluded_without_losing_known_totals(self):
        invalid_values = [
            None,
            {},
            {"prompt_tokens": 10, "completion_tokens": 2},
            {"prompt_tokens": "10", "completion_tokens": 2, "total_tokens": 12},
            {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
            {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
            "not an object",
        ]
        message = {"role": "assistant", "content": "正常回答"}
        for invalid in invalid_values:
            with self.subTest(usage=invalid):
                llm = ChatLLM(key="test-key", base_url="https://example.invalid", model="test")
                replies = [response(message, usage=usage(10, 2)),
                           response(message, usage=invalid),
                           response(message, usage=usage(20, 3))]
                with patch("agent.urllib.request.urlopen", side_effect=replies):
                    llm.chat([])
                    self.assertEqual(llm.chat([]), message)
                    llm.chat([])
                self.assertEqual(llm.usage_calls, 3)
                self.assertEqual(llm.usage_unknown, 1)
                self.assertEqual(llm.usage_totals, usage(30, 5))

        self.assertIn("已知累计：输入 30 / 输出 5 / 合计 35（另有 1 次调用用量未知，未计入）",
                      self.output.getvalue())

    def test_explicit_zero_usage_is_a_known_measurement(self):
        message = {"role": "assistant", "content": ""}
        with patch("agent.urllib.request.urlopen", return_value=response(message, usage=usage(0, 0))):
            self.llm.chat([])

        self.assertEqual(self.llm.usage_calls, 1)
        self.assertEqual(self.llm.usage_unknown, 0)
        self.assertIn("本次：输入 0 / 输出 0 / 合计 0", self.output.getvalue())
        self.assertNotIn("未知", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
