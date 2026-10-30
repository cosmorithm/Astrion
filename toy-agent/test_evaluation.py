"""评测层的离线行为测试：使用脚本化模型和模拟 HTTP 响应。"""

import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import agent
from evaluation import JudgeRegistry, default_registry, evaluate_case
from evaluation.__main__ import main as evaluation_main


def tool_message(name, arguments, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]}


class ScriptedLLM:
    model = "scripted-test-model"

    def __init__(self, replies, summary="记录摘要"):
        self.replies = iter(replies)
        self.inputs = []
        self.summary = summary

    def chat(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    def summarize(self, transcript):
        return self.summary


def case_data():
    return {
        "id": "sum_test",
        "task": "读取 data.txt，计算和，将数字写入 result.txt。",
        "initial_files": {"data.txt": "2, 3, 4"},
        "judges": [{"name": "file_equals", "path": "result.txt", "expected": "9"}],
    }


def success_replies():
    return [
        tool_message("read_file", {"path": "data.txt"}, "read-1"),
        tool_message("calculator", {"expression": "2 + 3 + 4"}, "calc-1"),
        tool_message("write_file", {"path": "result.txt", "content": "9"}, "write-1"),
        tool_message("finish", {"answer": "已写入结果"}, "finish-1"),
    ]


def http_response(message, usage_marker=None):
    payload = {"choices": [{"message": message}]}
    if usage_marker is not None:
        payload["usage"] = usage_marker
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_complete_run_records_trace_artifacts_and_independent_judgment(self):
        original_input = (agent.SANDBOX / "data.txt").read_bytes()
        original_output = (agent.SANDBOX / "result.txt").read_bytes()
        llm = ScriptedLLM(success_replies())

        report = evaluate_case(case_data(), lambda: llm)

        run = report["run"]
        self.assertEqual(report["case_id"], "sum_test")
        self.assertEqual(run["result"], {"status": "completed", "answer": "已写入结果", "steps": 4})
        self.assertGreaterEqual(run["duration_seconds"], 0)
        self.assertEqual(run["artifacts"], {"data.txt": "2, 3, 4", "result.txt": "9"})
        self.assertEqual(report["case"]["initial_files"], {"data.txt": "2, 3, 4"})
        self.assertEqual(run["agent_config"]["max_steps"], 10)
        self.assertTrue(any(tool["function"]["name"] == "read_file" for tool in run["agent_config"]["tools"]))
        self.assertIsNone(run["token_usage"])  # 脚本化模型没有服务端用量。
        self.assertEqual(report["judgments"][0]["status"], "pass")
        tools = [event for event in run["trace"] if event["type"] == "tool_result"]
        self.assertEqual([event["name"] for event in tools], ["read_file", "calculator", "write_file", "finish"])
        self.assertEqual([event["tool_call_id"] for event in tools], ["read-1", "calc-1", "write-1", "finish-1"])
        self.assertTrue(all(event["outcome"] == "ok" for event in tools))
        self.assertEqual(run["trace"][-1]["type"], "run_end")
        self.assertEqual((agent.SANDBOX / "data.txt").read_bytes(), original_input)
        self.assertEqual((agent.SANDBOX / "result.txt").read_bytes(), original_output)

    def test_previous_result_does_not_make_a_new_task_pass(self):
        first = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))
        second = evaluate_case(case_data(), lambda: ScriptedLLM([
            tool_message("finish", {"answer": "我已经完成"}, "finish-only"),
        ]))

        self.assertEqual(first["judgments"][0]["status"], "pass")
        self.assertEqual(second["run"]["result"]["status"], "completed")
        self.assertNotIn("result.txt", second["run"]["artifacts"])
        self.assertEqual(second["judgments"][0]["status"], "fail")

    def test_agent_error_is_recorded_and_does_not_disappear_into_the_judge_result(self):
        for error in (RuntimeError("模型响应出错"), SystemExit("网络错误")):
            with self.subTest(error=type(error).__name__):
                report = evaluate_case(case_data(), lambda: ScriptedLLM([error]))
                run = report["run"]
                self.assertEqual(run["result"]["status"], "error")
                self.assertEqual(run["result"]["error"]["type"], type(error).__name__)
                self.assertEqual(run["result"]["steps"], 0)
                self.assertGreaterEqual(run["duration_seconds"], 0)
                self.assertEqual(run["trace"][0]["type"], "model_request")
                self.assertEqual(report["judgments"][0]["status"], "fail")

    def test_multiple_custom_judges_cannot_mutate_each_others_record(self):
        registry = default_registry()

        def mutate(case, record, spec):
            record["artifacts"]["result.txt"] = "修改"
            case["judges"].clear()
            return {"status": "pass", "reason": "自定义裁判", "score": 0.75}

        def broken(case, record, spec):
            raise RuntimeError("裁判服务不可用")

        registry.register("mutate", mutate)
        registry.register("broken", broken)
        case = case_data()
        case["judges"] = [{"name": "mutate"}, *case["judges"], {"name": "broken"}, {"name": "missing"}]

        report = evaluate_case(case, lambda: ScriptedLLM(success_replies()), registry)

        self.assertEqual([item["status"] for item in report["judgments"]], ["pass", "pass", "error", "error"])
        self.assertEqual(report["judgments"][0]["score"], 0.75)
        self.assertIn("裁判服务不可用", report["judgments"][2]["reason"])
        self.assertEqual(report["run"]["artifacts"]["result.txt"], "9")
        self.assertEqual(report["run"]["result"]["status"], "completed")
        self.assertTrue(all(item["duration_seconds"] >= 0 for item in report["judgments"]))

    def test_judge_runs_after_agent_and_after_temporary_sandbox_is_removed(self):
        registry = JudgeRegistry()
        sandboxes = []
        real_run = agent.run

        def observe_sandbox(*args, **kwargs):
            sandboxes.append(kwargs["sandbox"])
            return real_run(*args, **kwargs)

        def judge(case, record, spec):
            self.assertEqual(record["result"]["status"], "completed")
            self.assertEqual(record["trace"][-1]["type"], "run_end")
            self.assertFalse(sandboxes[0].exists())
            return {"status": "pass", "reason": "裁判在结束后检查快照"}

        registry.register("after_run", judge)
        case = case_data()
        case["judges"] = [{"name": "after_run"}]
        with patch("evaluation.runner.agent.run", side_effect=observe_sandbox):
            report = evaluate_case(case, lambda: ScriptedLLM(success_replies()), registry)

        self.assertEqual(report["judgments"][0]["status"], "pass")

    def test_unreadable_artifact_is_unknown_not_task_failure(self):
        registry = default_registry()
        record = {"artifacts": {}, "artifact_errors": {"result.txt": "PermissionError: 无法读取"}}

        results = registry.evaluate(case_data(), record)

        self.assertEqual(results[0]["status"], "unknown")
        self.assertEqual(results[0]["evidence"]["path"], "result.txt")

    def test_judge_reference_is_not_sent_to_the_model(self):
        case = case_data()
        case["judges"][0]["expected"] = "ONLY_FOR_THE_JUDGE"
        llm = ScriptedLLM([tool_message("finish", {"answer": "结束"}, "finish-1")])

        report = evaluate_case(case, lambda: llm)

        self.assertNotIn("ONLY_FOR_THE_JUDGE", json.dumps(llm.inputs, ensure_ascii=False))
        self.assertNotIn("ONLY_FOR_THE_JUDGE", json.dumps(report["run"]["trace"], ensure_ascii=False))

    def test_actual_usage_is_per_run_and_missing_usage_stays_unknown(self):
        llm = agent.ChatLLM(key="test-key", base_url="https://example.invalid", model="test")
        llm.usage_totals = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        llm.usage_calls = 2
        llm.usage_unknown = 1
        replies = [
            http_response(tool_message("calculator", {"expression": "2+3"}, "calc"),
                          {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
            http_response(tool_message("finish", {"answer": "结束"}, "finish")),
        ]
        with patch("agent.urllib.request.urlopen", side_effect=replies):
            report = evaluate_case(case_data(), lambda: llm)

        self.assertEqual(report["run"]["token_usage"], {
            "known_totals": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "reported_calls": 1, "unknown_calls": 1, "complete": False,
        })

    def test_summary_model_usage_is_included_in_agent_total(self):
        case = case_data()
        case["task"] = "原始任务" * 200
        llm = agent.ChatLLM(key="test-key", base_url="https://example.invalid", model="test")
        replies = [
            http_response(tool_message("read_file", {"path": "data.txt"}, "read"),
                          {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
            http_response({"role": "assistant", "content": "已读取数据"},
                          {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}),
            http_response(tool_message("finish", {"answer": "结束"}, "finish"),
                          {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}),
        ]
        with patch("agent.urllib.request.urlopen", side_effect=replies):
            report = evaluate_case(case, lambda: llm, budget_chars=400)

        self.assertEqual(report["run"]["token_usage"], {
            "known_totals": {"prompt_tokens": 33, "completion_tokens": 7, "total_tokens": 40},
            "reported_calls": 3, "unknown_calls": 0, "complete": True,
        })
        self.assertIn("summary_request", [event["type"] for event in report["run"]["trace"]])
        self.assertEqual(report["run"]["result"]["steps"], 2)

    def test_time_measurement_does_not_impose_a_time_limit(self):
        with patch("evaluation.runner.perf_counter", side_effect=[10.0, 1210.0]):
            report = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))

        self.assertEqual(report["run"]["duration_seconds"], 1200.0)
        self.assertEqual(report["run"]["result"]["status"], "completed")
        self.assertEqual(report["judgments"][0]["status"], "pass")
        self.assertNotIn("max_duration_seconds", report)

    def test_trace_survives_memory_compaction(self):
        case = case_data()
        case["task"] = "原始任务" * 200
        llm = ScriptedLLM([
            tool_message("read_file", {"path": "data.txt"}, "read-1"),
            tool_message("finish", {"answer": "结束"}, "finish-1"),
        ])

        report = evaluate_case(case, lambda: llm, budget_chars=400)

        trace = report["run"]["trace"]
        kinds = [event["type"] for event in trace]
        self.assertIn("summary_request", kinds)
        self.assertIn("summary_response", kinds)
        self.assertIn("memory_compaction", kinds)
        first_request = next(event for event in trace if event["type"] == "model_request")
        self.assertEqual(first_request["messages"][-1]["content"], case["task"])
        self.assertEqual(report["run"]["result"]["status"], "completed")

    def test_case_cannot_create_files_outside_its_sandbox(self):
        case = case_data()
        case["initial_files"] = {"../escape.txt": "should not be written"}
        with self.assertRaises(PermissionError):
            evaluate_case(case, lambda: ScriptedLLM([]))

    def test_agent_tool_cannot_access_files_outside_its_evaluation_sandbox(self):
        llm = ScriptedLLM([
            tool_message("read_file", {"path": "../outside.txt"}, "read-outside"),
            tool_message("finish", {"answer": "结束"}, "finish-1"),
        ])
        report = evaluate_case(case_data(), lambda: llm)
        errors = [event for event in report["run"]["trace"]
                  if event["type"] == "tool_result" and event["tool_call_id"] == "read-outside"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["outcome"], "error")
        self.assertIn("PermissionError", errors[0]["content"])

    def test_invalid_judge_result_is_reported_as_judge_error(self):
        registry = JudgeRegistry()
        registry.register("bad", lambda case, record, spec: {"status": "not-a-status"})
        case = case_data()
        case["judges"] = [{"name": "bad"}]
        report = evaluate_case(case, lambda: ScriptedLLM([
            tool_message("finish", {"answer": "结束"}, "finish-1"),
        ]), registry)

        self.assertEqual(report["judgments"][0]["status"], "error")
        self.assertEqual(report["run"]["result"]["status"], "completed")

    def test_cli_prints_parseable_json_and_sends_agent_logs_to_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        llm = ScriptedLLM([tool_message("finish", {"answer": "结束"}, "finish-1")])
        with patch("sys.argv", ["evaluation", "--case", "sum_001"]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", return_value=llm), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            evaluation_main()

        report = json.loads(stdout.getvalue())
        self.assertEqual(report["reports"][0]["case_id"], "sum_001")
        self.assertIn("任务:", stderr.getvalue())
        self.assertNotIn("test-key", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
