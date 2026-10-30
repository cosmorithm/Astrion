"""评测层的离线行为测试：使用脚本化模型和模拟 HTTP 响应。"""

import copy
import io
import json
import os
import re
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
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
        storage = tempfile.TemporaryDirectory(prefix="astrion-agent-test-reports-")
        self.addCleanup(storage.cleanup)
        self.reports_dir = (Path(storage.name) / "reports").resolve()
        report_dir_patch = patch("evaluation.reporting.DEFAULT_REPORT_DIR", self.reports_dir)
        report_dir_patch.start()
        self.addCleanup(report_dir_patch.stop)
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
        self.assertEqual(run["event_schema_version"], 1)
        self.assertTrue(run["run_id"].startswith("run_"))
        self.assertTrue(all(event["run_id"] == run["run_id"] for event in run["trace"]))
        tools = [event for event in run["trace"]
                 if event["type"] == "tool.call.finished"]
        self.assertEqual([event["payload"]["name"] for event in tools],
                         ["read_file", "calculator", "write_file", "finish"])
        self.assertEqual([event["payload"]["tool_call_id"] for event in tools],
                         ["read-1", "calc-1", "write-1", "finish-1"])
        self.assertTrue(all(event["payload"]["outcome"] == "success" for event in tools))
        self.assertEqual(run["trace"][-1]["type"], "run.finished")
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
                self.assertEqual(run["trace"][0]["type"], "run.started")
                self.assertEqual(run["trace"][-1]["type"], "model.call.failed")
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
            self.assertEqual(record["trace"][-1]["type"], "run.finished")
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
        self.assertTrue(any(
            event["type"] == "model.call.started"
            and event["payload"]["purpose"] == "compaction"
            for event in report["run"]["trace"]
        ))
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
        self.assertIn("memory.compaction.started", kinds)
        self.assertIn("memory.compaction.finished", kinds)
        compaction_calls = [event for event in trace
                            if event["type"] == "model.call.started"
                            and event["payload"]["purpose"] == "compaction"]
        self.assertEqual(len(compaction_calls), 1)
        first_decision = next(event for event in trace
                              if event["type"] == "model.call.started"
                              and event["payload"]["purpose"] == "decision")
        self.assertNotIn("messages", first_decision["payload"]["input"])
        self.assertGreater(first_decision["payload"]["input"]["message_count"], 0)
        self.assertEqual(report["run"]["result"]["status"], "completed")

    def test_case_cannot_create_files_outside_its_sandbox(self):
        case = case_data()
        case["initial_files"] = {"../escape.txt": "should not be written"}
        report = evaluate_case(case, lambda: ScriptedLLM([]))
        self.assertEqual(report["run"]["result"]["status"], "setup_error")
        self.assertEqual(report["run"]["result"]["error"]["type"], "PermissionError")
        self.assertIsNone(report["run"]["duration_seconds"])
        self.assertEqual(report["judgments"][0]["status"], "unknown")
        self.assertEqual(json.loads(Path(report["report_path"]).read_text(encoding="utf-8")), report)

    def test_agent_tool_cannot_access_files_outside_its_evaluation_sandbox(self):
        llm = ScriptedLLM([
            tool_message("read_file", {"path": "../outside.txt"}, "read-outside"),
            tool_message("finish", {"answer": "结束"}, "finish-1"),
        ])
        report = evaluate_case(case_data(), lambda: llm)
        errors = [event for event in report["run"]["trace"]
                  if event["type"] == "tool.call.finished"
                  and event["payload"]["tool_call_id"] == "read-outside"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["payload"]["outcome"], "error")
        self.assertIn("PermissionError", errors[0]["payload"]["content"])

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
        self.assertEqual(Path(report["reports"][0]["report_path"]).parent, self.reports_dir)
        self.assertEqual(json.loads(Path(report["reports"][0]["report_path"]).read_text(encoding="utf-8")), report["reports"][0])
        self.assertIn("任务:", stderr.getvalue())
        self.assertIn("报告已保存", stderr.getvalue())
        self.assertIn(report["reports"][0]["report_path"], stderr.getvalue())
        self.assertEqual(report["summary"]["total_cases"], 1)
        self.assertEqual(report["summary"]["failed_cases"], 1)
        self.assertEqual(json.loads(Path(report["summary"]["report_path"]).read_text(encoding="utf-8")), report["summary"])
        self.assertNotIn("test-key", stdout.getvalue())

    def test_each_run_automatically_saves_a_distinct_complete_report(self):
        first = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))
        second = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))

        first_path, second_path = Path(first["report_path"]), Path(second["report_path"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.parent, self.reports_dir)
        self.assertEqual(second_path.parent, self.reports_dir)
        self.assertEqual(json.loads(first_path.read_text(encoding="utf-8")), first)
        self.assertEqual(json.loads(second_path.read_text(encoding="utf-8")), second)
        self.assertIsNone(first["report_error"])
        self.assertEqual(len(list(self.reports_dir.glob("*.json"))), 2)

    def test_agent_failure_still_saves_its_trace_and_error(self):
        report = evaluate_case(case_data(), lambda: ScriptedLLM([RuntimeError("连接中断")]))

        self.assertEqual(report["run"]["result"]["status"], "error")
        saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved, report)
        self.assertEqual(saved["run"]["result"]["error"]["message"], "连接中断")
        self.assertEqual(saved["run"]["trace"][0]["type"], "run.started")
        self.assertEqual(saved["run"]["trace"][-1]["type"], "model.call.failed")

    def test_filename_collision_is_retried_without_overwriting_old_report(self):
        fixed_time = datetime(2026, 10, 30, 1, 2, 3, tzinfo=timezone.utc)
        with patch("evaluation.reporting.datetime") as clock, \
             patch("evaluation.reporting.uuid.uuid4", side_effect=[uuid.UUID(int=1), uuid.UUID(int=1), uuid.UUID(int=2)]):
            clock.now.return_value = fixed_time
            first = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))
            original = Path(first["report_path"]).read_bytes()
            second = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))

        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertEqual(Path(first["report_path"]).read_bytes(), original)
        self.assertEqual(json.loads(Path(second["report_path"]).read_text(encoding="utf-8")), second)

    def test_setup_failure_is_saved_without_running_a_judge(self):
        registry = JudgeRegistry()
        judge_calls = []
        registry.register("observe", lambda case, record, spec: judge_calls.append(True))
        case = case_data()
        case["judges"] = [{"name": "observe"}]

        def broken_factory():
            raise RuntimeError("模型初始化失败")

        report = evaluate_case(case, broken_factory, registry)

        self.assertEqual(report["run"]["result"]["status"], "setup_error")
        self.assertEqual(report["run"]["result"]["error"]["message"], "模型初始化失败")
        self.assertIsNone(report["run"]["duration_seconds"])
        self.assertEqual(report["judgments"][0]["status"], "unknown")
        self.assertEqual(judge_calls, [])
        self.assertEqual(json.loads(Path(report["report_path"]).read_text(encoding="utf-8")), report)

    def test_case_id_cannot_escape_report_directory(self):
        case = case_data()
        case["id"] = "../../中文/任务"
        report = evaluate_case(case, lambda: ScriptedLLM(success_replies()))

        self.assertEqual(Path(report["report_path"]).parent, self.reports_dir)
        self.assertEqual(json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))["case_id"], case["id"])

    def test_failed_report_save_preserves_result_and_reports_the_error(self):
        with patch("evaluation.runner.save_report", side_effect=OSError("磁盘不可用")):
            report = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))

        self.assertIsNone(report["report_path"])
        self.assertEqual(report["report_error"], {"type": "OSError", "message": "磁盘不可用"})
        self.assertEqual(report["run"]["result"]["status"], "completed")
        self.assertEqual(report["judgments"][0]["status"], "pass")

    def test_unusable_report_directory_is_reported_without_overwriting_it(self):
        blocking_file = self.reports_dir.parent / "not-a-directory"
        blocking_file.write_text("原有内容", encoding="utf-8")
        report = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()),
                               report_dir=blocking_file / "reports")

        self.assertIsNone(report["report_path"])
        self.assertIn(report["report_error"]["type"], {"NotADirectoryError", "FileExistsError", "OSError"})
        self.assertEqual(report["run"]["result"]["status"], "completed")
        self.assertEqual(blocking_file.read_text(encoding="utf-8"), "原有内容")

    def test_partial_report_is_removed_when_writing_fails(self):
        original_open = Path.open
        reports_dir = self.reports_dir

        class BrokenWriter:
            def __init__(self, output):
                self.output = output

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.output.close()

            def write(self, payload):
                self.output.write(payload[:10])
                raise OSError("写入中断")

        def open_with_failure(path, *args, **kwargs):
            output = original_open(path, *args, **kwargs)
            if path.parent == reports_dir and path.suffix == ".json":
                return BrokenWriter(output)
            return output

        with patch("pathlib.Path.open", new=open_with_failure):
            report = evaluate_case(case_data(), lambda: ScriptedLLM(success_replies()))

        self.assertIsNone(report["report_path"])
        self.assertIn("写入中断", report["report_error"]["message"])
        self.assertEqual(list(self.reports_dir.glob("*.json")), [])

    def test_cli_custom_directory_saves_one_report_per_case(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        case_a, case_b = case_data(), case_data()
        case_a["id"], case_b["id"] = "first", "second"
        cases_path = self.reports_dir.parent / "cases.json"
        cases_path.write_text(json.dumps([case_a, case_b]), encoding="utf-8")
        custom_dir = self.reports_dir.parent / "custom-reports"
        llms = [ScriptedLLM(success_replies()), ScriptedLLM(success_replies())]
        with patch("sys.argv", ["evaluation", "--cases", str(cases_path), "--report-dir", str(custom_dir)]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", side_effect=llms), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            evaluation_main()

        output = json.loads(stdout.getvalue())
        reports = output["reports"]
        self.assertEqual([report["case_id"] for report in reports], ["first", "second"])
        self.assertEqual(len({report["report_path"] for report in reports}), 2)
        self.assertEqual(stderr.getvalue().count("报告已保存（"), 2)
        self.assertEqual(output["summary"]["pass_rate_percent"], 100.0)
        self.assertEqual(len(list(custom_dir.glob("*.json"))), 3)
        self.assertEqual(json.loads(Path(output["summary"]["report_path"]).read_text(encoding="utf-8")), output["summary"])
        for report in reports:
            path = Path(report["report_path"])
            self.assertEqual(path.parent, custom_dir)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_batch_saves_setup_error_and_continues_with_next_case(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        broken, valid = case_data(), case_data()
        broken["id"], valid["id"] = "broken", "valid"
        broken["initial_files"] = {"../escape.txt": "forbidden"}
        cases_path = self.reports_dir.parent / "mixed-cases.json"
        cases_path.write_text(json.dumps([broken, valid]), encoding="utf-8")
        with patch("sys.argv", ["evaluation", "--cases", str(cases_path)]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", return_value=ScriptedLLM(success_replies())), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            evaluation_main()

        output = json.loads(stdout.getvalue())
        reports = output["reports"]
        self.assertEqual([report["run"]["result"]["status"] for report in reports], ["setup_error", "completed"])
        self.assertEqual([report["judgments"][0]["status"] for report in reports], ["unknown", "pass"])
        self.assertTrue(all(Path(report["report_path"]).is_file() for report in reports))
        self.assertEqual(output["summary"]["unresolved_cases"], 1)
        self.assertEqual(output["summary"]["pass_rate_percent"], 50.0)
        self.assertEqual(output["summary"]["scored_pass_rate_percent"], 100.0)

    def test_cli_default_runs_all_ten_cases_and_saves_a_summary(self):
        cases_path = Path(__file__).resolve().parent / "evaluation" / "cases.json"
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        llms = []
        for case in cases:
            replies = [tool_message("write_file", {"path": spec["path"], "content": spec["expected"]},
                                    f"write-{index}") for index, spec in enumerate(case["judges"])]
            replies.append(tool_message("finish", {"answer": "完成"}, "finish"))
            llms.append(ScriptedLLM(replies))

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["evaluation"]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", side_effect=llms), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            evaluation_main()

        output = json.loads(stdout.getvalue())
        self.assertEqual(len(output["reports"]), 10)
        self.assertEqual(output["summary"]["total_cases"], 10)
        self.assertEqual(output["summary"]["passed_cases"], 10)
        self.assertEqual(output["summary"]["pass_rate_percent"], 100.0)
        self.assertEqual(len(list(self.reports_dir.glob("*.json"))), 11)
        self.assertTrue(all(Path(report["report_path"]).is_file() for report in output["reports"]))
        self.assertTrue(Path(output["summary"]["report_path"]).is_file())
        self.assertIn("通过 10/10", stderr.getvalue())

    def test_cli_returns_nonzero_but_keeps_json_when_saving_fails(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        llm = ScriptedLLM([tool_message("finish", {"answer": "结束"}, "finish-1")])
        with patch("sys.argv", ["evaluation", "--case", "sum_001"]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", return_value=llm), \
             patch("evaluation.runner.save_report", side_effect=OSError("磁盘不可用")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                evaluation_main()

        self.assertEqual(exc.exception.code, 1)
        report = json.loads(stdout.getvalue())["reports"][0]
        self.assertEqual(report["run"]["result"]["status"], "completed")
        self.assertIsNone(report["report_path"])
        self.assertIn("磁盘不可用", report["report_error"]["message"])
        self.assertIn("报告保存失败", stderr.getvalue())

    def test_cli_summary_save_failure_keeps_individual_report_and_results(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        llm = ScriptedLLM([tool_message("finish", {"answer": "结束"}, "finish-1")])
        with patch("sys.argv", ["evaluation", "--case", "sum_001"]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("agent.ChatLLM", return_value=llm), \
             patch("evaluation.suite.save_report", side_effect=OSError("汇总目录不可用")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                evaluation_main()

        self.assertEqual(exc.exception.code, 1)
        output = json.loads(stdout.getvalue())
        self.assertTrue(Path(output["reports"][0]["report_path"]).is_file())
        self.assertIsNone(output["summary"]["report_path"])
        self.assertIn("汇总目录不可用", output["summary"]["report_error"]["message"])
        self.assertIn("总报告保存失败", stderr.getvalue())

    def test_cli_explains_when_a_result_cannot_be_serialized(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        invalid_report = {"case_id": "sum_001", "report_path": None,
                          "report_error": {"type": "TypeError", "message": "无法序列化"},
                          "run": {"invalid": object()}}
        with patch("sys.argv", ["evaluation", "--case", "sum_001"]), \
             patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}), \
             patch("agent.load_env"), patch("evaluation.__main__.evaluate_case", return_value=invalid_report), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                evaluation_main()

        self.assertEqual(exc.exception.code, 1)
        self.assertIn("报告保存失败", stderr.getvalue())
        self.assertIn("评测结果无法输出为 JSON", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_example_cases_have_correct_targets_and_clean_starting_files(self):
        cases_path = Path(__file__).resolve().parent / "evaluation" / "cases.json"
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        by_id = {case["id"]: case for case in cases}
        self.assertEqual(len(cases), 10)
        self.assertEqual(len(by_id), len(cases))
        for case in cases:
            for spec in case["judges"]:
                self.assertNotIn(spec["path"], case["initial_files"])

        input_paths = {
            "sum_001": "data.txt", "sum_negative": "data.txt", "sum_decimal_2dp": "data.txt",
            "sum_multiline": "data.txt", "sum_fullwidth": "data.txt", "sum_empty": "data.txt",
            "nested_sum": "records/numbers.txt",
        }
        pattern = r"[+-]?\d+(?:\.\d+)?"
        for case_id, path in input_paths.items():
            with self.subTest(case_id=case_id):
                case = by_id[case_id]
                numbers = [Decimal(value) for value in re.findall(pattern, case["initial_files"][path])]
                self.assertEqual(Decimal(case["judges"][0]["expected"]), sum(numbers, Decimal(0)))
        self.assertRegex(by_id["sum_decimal_2dp"]["judges"][0]["expected"], r"^-?\d+\.\d{2}$")
        self.assertEqual(by_id["copy_text"]["judges"][0]["expected"],
                         by_id["copy_text"]["initial_files"]["note.txt"])
        listed = sorted(name for name in by_id["list_txt_files"]["initial_files"] if name.endswith(".txt"))
        self.assertEqual(by_id["list_txt_files"]["judges"][0]["expected"], ",".join(listed))
        combined = by_id["sum_and_count"]
        numbers = [int(value) for value in re.findall(pattern, combined["initial_files"]["data.txt"])]
        expected = {judge["path"]: judge["expected"] for judge in combined["judges"]}
        self.assertEqual(expected, {"output/sum.txt": str(sum(numbers)), "output/count.txt": str(len(numbers))})

    def test_list_cases_needs_no_api_key_or_model(self):
        stdout = io.StringIO()
        with patch("sys.argv", ["evaluation", "--list"]), \
             patch("agent.load_env") as load_env, patch("agent.ChatLLM") as model, \
             redirect_stdout(stdout):
            evaluation_main()

        listed = json.loads(stdout.getvalue())["cases"]
        self.assertEqual(len(listed), 10)
        self.assertTrue(any(case["id"] == "sum_and_count" for case in listed))
        load_env.assert_not_called()
        model.assert_not_called()
        self.assertFalse(self.reports_dir.exists())


if __name__ == "__main__":
    unittest.main()
