import json
import unittest
from unittest.mock import patch

from evaluation.context.live_recall import (
    answer_correct,
    contains_value,
    fixtures,
    run_one,
    visibility_timeline,
)
from tests.context_counter import CodepointCounter


def call(name, arguments, ident):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": ident,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.last_usage = None
        self.usage_calls = 0
        self.usage_unknown = 0
        self.usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.decisions = 0

    def count(self):
        self.usage_calls += 1
        self.last_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
        for key, value in self.last_usage.items():
            self.usage_totals[key] += value

    def summarize(self, transcript):
        self.count()
        return json.dumps(
            {"progress": "工作准备完成", "pending": "回答问题", "evidence_ids": []},
            ensure_ascii=False,
        )

    def chat(self, messages, tools=None):
        self.count()
        system = messages[0]["content"]
        if system.startswith("You are summarizing"):
            return {"content": json.dumps({"summary": "记录已归档"})}
        if system.startswith("You select past"):
            return {"content": json.dumps({"selections": []})}
        if system.startswith("Select historical phases"):
            phases = json.loads(messages[1]["content"])["phases"]
            return {"content": json.dumps({"phases": [p["phase_id"] for p in phases]})}
        self.decisions += 1
        if self.decisions == 1:
            return call("search_step_history", {"query": "最初纳入"}, "one")
        if self.decisions == 2:
            return call(
                "read_step_history",
                {"step_id": "fixed_L01:step:1", "level": "RAW"},
                "two",
            )
        return call("finish", {"answer": "148"}, "three")


class LiveRecallTests(unittest.TestCase):
    def test_fixture_positions_and_matching(self):
        cases, _, _ = fixtures()
        self.assertEqual(
            [c["spec"]["position"] for c in cases], ["early", "middle", "late"]
        )
        self.assertTrue(contains_value("数值为 0.005。", "0.005"))
        self.assertFalse(contains_value("数值为 0.0005。", "0.005"))
        self.assertFalse(answer_correct("1480", "148"))

    def test_reconstruction_before_tool_is_not_tool_first_discovery(self):
        trace = [
            {
                "type": "model.call.started",
                "payload": {
                    "purpose": "decision",
                    "request_snapshot": {
                        "messages": [{"role": "user", "content": "question"}]
                    },
                },
            },
            {"type": "memory.compaction.finished", "payload": {"compacted": True}},
            {
                "type": "model.call.started",
                "payload": {
                    "purpose": "decision",
                    "request_snapshot": {
                        "messages": [{"role": "assistant", "content": "sample 148"}]
                    },
                },
            },
            {
                "type": "tool.call.finished",
                "payload": {
                    "name": "read_step_history",
                    "outcome": "success",
                    "content": "148",
                },
            },
        ]
        result = visibility_timeline(trace, "148", False)
        self.assertEqual(result["first_observed"], "later_decision_context")
        self.assertLess(result["first_decision_index"], result["first_tool_index"])

    def test_real_agent_uses_scoped_tools_to_recover_omitted_fact(self):
        case = fixtures()[0][0]
        counter = CodepointCounter()
        with patch(
            "evaluation.context.live_recall.load_token_counter", return_value=counter
        ), patch("agent.load_token_counter", return_value=counter):
            result = run_one(case, "ss", FakeLLM(), max_steps=5)
        self.assertEqual(result["construction"]["status"], "applied")
        self.assertFalse(result["construction"]["expected_literal_visible"])
        self.assertEqual(
            [x["name"] for x in result["retrieval"]],
            ["search_step_history", "read_step_history"],
        )
        self.assertTrue(result["expected_observed_in_tool"])
        self.assertTrue(result["correct"])
        self.assertTrue(result["tool_recovery_and_correct"])
        self.assertEqual(result["visibility_timeline"]["first_observed"], "tool_result")
        self.assertTrue(result["tool_first_recovery_and_correct"])
        self.assertTrue(result["usage"]["agent_retry_tracking"])
        self.assertIn("records", result["archive_snapshot"])
        self.assertTrue(
            all(
                c["local_input_tokens"] <= result["budget"]["local_budget_tokens"]
                for c in result["construction_calls"]
                if c["sent"]
            )
        )

    def test_failed_model_construction_is_unfinished_not_wrong_answer(self):
        class Failing(FakeLLM):
            def summarize(self, transcript):
                raise SystemExit("synthetic upstream error")

        counter = CodepointCounter()
        with patch(
            "evaluation.context.live_recall.load_token_counter", return_value=counter
        ), patch("agent.load_token_counter", return_value=counter):
            result = run_one(fixtures()[0][0], "legacy", Failing(), max_steps=5)
        self.assertEqual(result["status"], "construction_failed")
        self.assertIsNone(result["correct"])
        self.assertFalse(result["usage"]["complete"])
        self.assertEqual(result["trace"], [])

    def test_legacy_no_answer_in_view_is_not_automatically_called_a_failure(self):
        case = fixtures()[0][0]
        counter = CodepointCounter()
        with patch(
            "evaluation.context.live_recall.load_token_counter", return_value=counter
        ), patch("agent.load_token_counter", return_value=counter):
            result = run_one(case, "legacy", FakeLLM(), max_steps=5)
        self.assertEqual(result["construction"]["status"], "applied")
        self.assertTrue(result["correct"])
        self.assertTrue(result["tool_recovery_and_correct"])


if __name__ == "__main__":
    unittest.main()
