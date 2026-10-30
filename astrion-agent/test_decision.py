"""Astrion 增强决策及 BFCL 对接的离线回归。"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import agent
from benchmarks.bfcl import BUILTIN_BENCHMARKS, DEFAULT_MODEL, PINNED_BFCL_VERSION, load_manifest, run_astrion_bfcl
from benchmarks.bfcl_astrion import GENERATION_REPORT, generate, run_enhanced_case
from benchmarks.common import FunctionCallCase
from decision import enhanced_decision, validate_candidate
from events import EventRecorder
from memory import Memory


def response(text=None, calls=None):
    result = {"role": "assistant", "content": text}
    if calls is not None:
        result["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
            "name": name, "arguments": raw}} for i, (name, raw) in enumerate(calls)]
    return result


TOOLS = [{"type": "function", "function": {"name": "get_time", "description": "get local time",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"], "additionalProperties": False}}}]
PLAN = response('{"subtasks":["Tokyo", "New York"],"missing_information":[],"tool_assessment":"get_time"}')
REVIEW_OK = response('{"needs_revision":false,"issues":[]}')


class ScriptedLLM:
    model = "test-model"
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.last_usage = None
    def chat(self, messages, tools=None):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        return copy.deepcopy(next(self.responses))


class DecisionTests(unittest.TestCase):
    def test_plan_review_and_repair_use_same_core_and_no_fabricated_tool_results(self):
        initial = response(calls=[("get_time", '{"city": 123}')])
        final = response(calls=[("get_time", '{"city":"Tokyo"}'), ("get_time", '{"city":"New York"}')])
        llm = ScriptedLLM([PLAN, initial, REVIEW_OK, final])
        decisions = []
        def caller(messages, tools, purpose):
            decisions.append(purpose)
            return llm.chat(messages, tools)
        result = enhanced_decision([{"role": "user", "content": "Tokyo and New York"}], TOOLS, caller)
        self.assertEqual(decisions, ["decision.plan", "decision.candidate", "decision.review", "decision.repair"])
        self.assertTrue(result["repair_attempted"])
        self.assertTrue(result["initial_validation"]["issues"])
        self.assertFalse(result["final_validation"]["issues"])
        self.assertEqual(result["message"], final)
        self.assertEqual(llm.requests[0][1], [])
        self.assertEqual(llm.requests[1][1], TOOLS)
        self.assertFalse(any(m.get("role") == "tool" for messages, _ in llm.requests for m in messages))

    def test_valid_no_tool_response_is_preserved_without_repair(self):
        llm = ScriptedLLM([PLAN, response("No suitable tool"), REVIEW_OK])
        result = enhanced_decision([{"role": "user", "content": "question"}], TOOLS,
                                   lambda messages, tools, _purpose: llm.chat(messages, tools))
        self.assertEqual(result["message"]["content"], "No suitable tool")
        self.assertFalse(result["repair_attempted"])
        self.assertEqual(len(llm.requests), 3)

    def test_unsupported_schema_is_reported_without_claiming_full_validation(self):
        tools = copy.deepcopy(TOOLS)
        tools[0]["function"]["parameters"]["properties"]["city"]["pattern"] = "^A"
        check = validate_candidate(response(calls=[("get_time", '{"city":"Tokyo"}')]), tools)
        self.assertFalse(check["issues"])
        self.assertTrue(any("pattern" in item for item in check["unchecked"]))

    def test_enhanced_bfcl_case_accounts_for_all_calls(self):
        case = FunctionCallCase("simple_python_0", "simple_python", [{"role": "user", "content": "Tokyo"}], TOOLS)
        llm = ScriptedLLM([PLAN, response(calls=[("get_time", '{"city":12}')]), REVIEW_OK,
                           response(calls=[("get_time", '{"city":"Tokyo"}')])])
        with tempfile.TemporaryDirectory() as temp:
            official, record = run_enhanced_case(case, llm, Path(temp))
            self.assertEqual(official["input_token_count"], 40)
            self.assertEqual(official["output_token_count"], 8)
            self.assertEqual(record["model_call_count"], 4)
            self.assertTrue(record["decision"]["repair_attempted"])
            events = json.loads(Path(record["event_path"]).read_text())["events"]
            self.assertEqual(len([event for event in events if event["type"] == "model.call.finished"]), 4)
            self.assertFalse(any(event["type"].startswith("tool.call.") for event in events))

    def test_real_agent_uses_same_enhanced_decision_before_executing_tool(self):
        llm = ScriptedLLM([PLAN, response(calls=[("finish", '{"answer":"完成"}')]), REVIEW_OK])
        recorder = EventRecorder()
        with redirect_stdout(io.StringIO()):
            result = agent.run("任务", llm, Memory("规则"), decision_mode="enhanced", event_sink=recorder)
        self.assertEqual(result["status"], "completed")
        purposes = [e["payload"]["purpose"] for e in recorder.events if e["type"] == "model.call.started"]
        self.assertEqual(purposes, ["decision.plan", "decision.candidate", "decision.review"])
        self.assertTrue(any(e["type"] == "tool.call.finished" for e in recorder.events))

    def test_generate_saves_initial_and_final_separately(self):
        case = FunctionCallCase("simple_python_0", "simple_python", [{"role": "user", "content": "Tokyo"}], TOOLS)
        llm = ScriptedLLM([PLAN, response(calls=[("get_time", '{"city":12}')]), REVIEW_OK,
                           response(calls=[("get_time", '{"city":"Tokyo"}')])])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = lambda root, model, category: root / "result" / model / f"BFCL_v4_{category}_result.json"
            report = generate([case], llm, root, scorer_model=DEFAULT_MODEL, result_path=result_path, mode="enhanced")
            initial = json.loads(result_path(root / "initial", DEFAULT_MODEL, case.category).read_text())
            final = json.loads(result_path(root, DEFAULT_MODEL, case.category).read_text())
            self.assertEqual(initial["result"][0]["get_time"], '{"city":12}')
            self.assertEqual(final["result"][0]["get_time"], '{"city":"Tokyo"}')
            self.assertEqual(report["token_usage"]["known_total_tokens"], 48)
            self.assertEqual(report["repair_count"], 1)

    def test_parent_enhanced_runs_two_official_scorers_and_paired_comparison(self):
        manifest = load_manifest(BUILTIN_BENCHMARKS["trial-3"])
        def runner(command, env, cwd):
            result = {"command": command, "returncode": 0, "duration_seconds": 0.01, "stdout": "", "stderr": ""}
            if command[1] == "version":
                result["stdout"] = f"bfcl version: {PINNED_BFCL_VERSION}\n"
            elif command[1] == "models":
                result["stdout"] = DEFAULT_MODEL
            elif command[1].endswith("bfcl_astrion.py"):
                for target in (cwd, cwd / "initial"):
                    for category, ids in manifest.categories.items():
                        path = target / "result" / DEFAULT_MODEL / f"BFCL_v4_{category}_result.json"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("".join(json.dumps({"id": i, "result": [], "input_token_count": 1,
                                                            "output_token_count": 1, "latency": 0.1}) + "\n" for i in ids))
                (cwd / GENERATION_REPORT).write_text(json.dumps({"kind": "bfcl_astrion_generation", "status": "completed",
                    "execution_path": "astrion_enhanced_decision", "case_count": manifest.case_count, "error_count": 0}))
            elif command[1] == "evaluate":
                for category, ids in manifest.categories.items():
                    path = cwd / "score" / DEFAULT_MODEL / f"BFCL_v4_{category}_score.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    is_failed = cwd.name == "initial" and category == "simple_python"
                    path.write_text(json.dumps({"accuracy": 0 if is_failed else 1, "correct_count": 0 if is_failed else 1,
                                                "total_count": 1}) + "\n" + (json.dumps({"id": ids[0], "valid": False}) + "\n" if is_failed else ""))
            else:
                raise AssertionError(command)
            return result
        with tempfile.TemporaryDirectory() as temp:
            report = run_astrion_bfcl(api_model="deepseek-flash", bfcl_bin="/fake/bfcl", bfcl_python="/fake/python",
                                      manifest_path=manifest.path, output_dir=Path(temp) / "run", mode="enhanced",
                                      env={"DEEPSEEK_API_KEY": "test"}, command_runner=runner)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["initial_official_outputs"]["correct_cases"], 2)
            self.assertEqual(report["official_outputs"]["correct_cases"], 3)
            self.assertEqual(report["paired_comparison"]["improved"], 1)


if __name__ == "__main__":
    unittest.main()
