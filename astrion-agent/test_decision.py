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
from decision import audit_missing_information, audit_plan_evidence, audit_review_evidence, enhanced_decision, validate_candidate
from decision_prompts import PROMPT_VERSION, prompt_policy
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
PLAN = response(json.dumps({"schema_version": 2, "subtasks": ["Tokyo", "New York"], "parameter_evidence": [
    {"tool": "get_time", "parameter": "city", "value": "Tokyo", "source": "user_explicit",
     "quote": "Tokyo", "explanation": "User provided city"},
], "missing_information": [], "capability_gaps": [], "tool_assessment": "get_time"}))
REVIEW_OK = response('{"schema_version":2,"needs_revision":false,"issues":[]}')


def review_issue(description, **overrides):
    issue = {"type": "uncertainty", "description": description, "subtask_index": None,
             "user_quote": None, "tool": None, "parameter": None, "call_index": None}
    issue.update(overrides)
    return response(json.dumps({"schema_version": 2, "needs_revision": True, "issues": [issue]}))


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
        self.assertTrue(result["repair_accepted"])
        self.assertEqual(result["plan"]["parameter_evidence"][0]["source"], "user_explicit")
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

    def test_semantic_review_alone_does_not_overturn_valid_optional_default(self):
        tools = [{"type": "function", "function": {"name": "concert", "parameters": {
            "type": "object", "properties": {"artist": {"type": "string"},
            "month": {"type": "string"}, "year": {"type": "integer", "default": 2022}},
            "required": ["artist", "month"]}}}]
        candidate = response(calls=[("concert", '{"artist":"The Weeknd","month":"December"}')])
        review = review_issue("Ask for year")
        llm = ScriptedLLM([PLAN, candidate, review])
        result = enhanced_decision([{"role": "user", "content": "December concert"}], tools,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertEqual(result["message"], candidate)
        self.assertTrue(result["review"]["needs_revision"])
        self.assertTrue(result["semantic_review_advisory_only"])
        self.assertFalse(result["repair_attempted"])
        self.assertEqual(len(llm.requests), 3)

    def test_repair_cannot_change_unrelated_valid_tool_call(self):
        initial = response(calls=[("get_time", '{"city":"Tokyo"}'), ("get_time", '{"city":12}')])
        changed = response(calls=[("get_time", '{"city":"London"}'), ("get_time", '{"city":"New York"}')])
        llm = ScriptedLLM([PLAN, initial, REVIEW_OK, changed])
        result = enhanced_decision([{"role": "user", "content": "Tokyo and New York"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertTrue(result["repair_attempted"])
        self.assertFalse(result["repair_accepted"])
        self.assertIn("改变或丢失", result["repair_rejection"])
        self.assertEqual(result["message"], initial)
        self.assertTrue(result["final_validation"]["issues"])

    def test_repair_preserves_valid_calls_and_does_not_receive_semantic_suspicions(self):
        initial = response(calls=[("get_time", '{"city":"Tokyo"}'), ("get_time", '{"city":12}')])
        repaired = response(calls=[("get_time", '{"city":"Tokyo"}'), ("get_time", '{"city":"New York"}')])
        review = review_issue("SPECULATIVE: remove Tokyo")
        llm = ScriptedLLM([PLAN, initial, review, repaired])
        result = enhanced_decision([{"role": "user", "content": "Tokyo and New York"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertTrue(result["repair_accepted"])
        self.assertEqual(result["message"], repaired)
        self.assertNotIn("SPECULATIVE", json.dumps(llm.requests[-1][0]))
        self.assertEqual(result["review"]["issues"][0]["description"], "SPECULATIVE: remove Tokyo")

    def test_repair_that_still_violates_schema_is_rejected(self):
        initial = response(calls=[("get_time", '{"city":12}')])
        invalid_repair = response(calls=[("get_time", '{"city":false}')])
        llm = ScriptedLLM([PLAN, initial, REVIEW_OK, invalid_repair])
        result = enhanced_decision([{"role": "user", "content": "time"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertFalse(result["repair_accepted"])
        self.assertIn("仍有结构错误", result["repair_rejection"])
        self.assertEqual(result["message"], initial)

    def test_repair_cannot_replace_all_tool_calls_with_clarification(self):
        initial = response(calls=[("get_time", '{"city":12}')])
        llm = ScriptedLLM([PLAN, initial, REVIEW_OK, response("Please clarify")])
        result = enhanced_decision([{"role": "user", "content": "time"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertFalse(result["repair_accepted"])
        self.assertIn("删除了全部", result["repair_rejection"])
        self.assertEqual(result["message"], initial)

    def test_structured_missing_information_is_recorded_without_automatic_gating(self):
        plan = response(json.dumps({"schema_version": 2, "subtasks": ["query"], "parameter_evidence": [
            {"tool": "get_time", "parameter": "city", "value": None, "source": "unknown", "quote": None, "explanation": "not specified"}
        ], "missing_information": [{"tool": "get_time", "parameter": "city", "subtask_index": 0, "required": True, "default_available": False,
                                      "blocks_action": True, "reason": "city is required"}],
            "capability_gaps": [], "tool_assessment": "ask user"}))
        llm = ScriptedLLM([plan, response("Which city?"), REVIEW_OK])
        result = enhanced_decision([{"role": "user", "content": "time"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertIsNone(result["plan_error"])
        self.assertEqual(result["plan"]["missing_information"][0]["blocks_action"], True)
        self.assertEqual(result["plan"]["parameter_evidence"][0]["source"], "unknown")
        self.assertTrue(result["missing_information_audit"][0]["schema_required"])
        self.assertTrue(result["missing_information_audit"][0]["required_claim_matches"])
        self.assertTrue(result["missing_information_audit"][0]["default_claim_matches"])
        self.assertFalse(result["repair_attempted"])

    def test_invalid_plan_provenance_is_discarded_and_reported(self):
        llm = ScriptedLLM([response('{"subtasks":["test"],"parameter_evidence":"fabricated"}'),
                           response("No tool"), REVIEW_OK])
        result = enhanced_decision([{"role": "user", "content": "question"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertIsNotNone(result["plan_error"])
        self.assertEqual(result["plan"]["parameter_evidence"], [])

    def test_evidence_audit_checks_user_quotes_and_explicit_defaults(self):
        tools = [{"type": "function", "function": {"name": "search", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"},
            "year": {"type": "integer", "default": 2022}}}}}]
        plan = {"parameter_evidence": [
            {"tool": "search", "parameter": "city", "value": "Tokyo", "source": "user_explicit", "evidence": "Tokyo"},
            {"tool": "search", "parameter": "city", "value": "Paris", "source": "user_explicit", "evidence": "Paris"},
            {"tool": "search", "parameter": "year", "value": 2022, "source": "tool_default", "evidence": "default"},
            {"tool": "search", "parameter": "year", "value": 2023, "source": "tool_default", "evidence": "default"},
            {"tool": "search", "parameter": "city", "value": "Tokyo", "source": "inferred", "evidence": "assumption"},
        ]}
        audit = audit_plan_evidence(plan, [{"role": "user", "content": "Tokyo"},
                                           {"role": "assistant", "content": "Paris"}], tools)
        self.assertEqual([item["status"] for item in audit], ["quote_found", "quote_not_found", "default_matches",
                                                               "default_mismatch", "not_verifiable"])

    def test_quote_and_explanation_are_separate_and_only_user_quotes_are_matched(self):
        evidence = [
            {"tool": "get_time", "parameter": "city", "value": "Tokyo", "source": "user_explicit",
             "quote": '"Tokyo"', "explanation": "User asked about Tokyo"},
            {"tool": "get_time", "parameter": "city", "value": "Paris", "source": "user_explicit",
             "quote": None, "explanation": "Paris"},
            {"tool": "get_time", "parameter": "city", "value": "Paris", "source": "user_explicit",
             "quote": "Paris", "explanation": "Paris"},
            {"tool": "get_time", "parameter": "city", "value": "Tokyo", "source": "user_explicit",
             "quote": 'User asked about "Tokyo"', "explanation": "explanation is not a quote"},
        ]
        audit = audit_plan_evidence({"parameter_evidence": evidence}, [
            {"role": "user", "content": "Tokyo"}, {"role": "assistant", "content": "Paris"}], TOOLS)
        self.assertEqual(audit[0]["match_method"], "outer_quotes_removed")
        self.assertEqual(audit[0]["matched_message_indices"], [0])
        self.assertEqual([a["status"] for a in audit], ["quote_found", "quote_not_supplied", "quote_not_found", "quote_not_found"])

    def test_capability_gap_is_separate_from_schema_missing_parameter(self):
        plan = response(json.dumps({"schema_version": 2, "subtasks": ["route"], "parameter_evidence": [],
            "missing_information": [], "capability_gaps": [{"subtask_index": 0, "tool": None,
            "missing_capability": "route comparison", "reason": "only airport lookup"}], "tool_assessment": "insufficient"}))
        llm = ScriptedLLM([plan, response("No route tool"), REVIEW_OK])
        result = enhanced_decision([{"role": "user", "content": "fastest route"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertIsNone(result["plan_error"])
        self.assertEqual(result["plan"]["missing_information"], [])
        self.assertEqual(result["plan"]["capability_gaps"][0]["missing_capability"], "route comparison")
        self.assertIsNone(result["capability_gaps_audit"][0]["tool_known"])

    def test_review_evidence_is_audited_but_never_used_as_automatic_repair(self):
        candidate = response(calls=[("get_time", '{"city":"Tokyo"}')])
        review = review_issue("Potential missing goal", type="missing_goal", subtask_index=1,
                              user_quote="New York", tool="wrong_tool", parameter="city", call_index=3)
        llm = ScriptedLLM([PLAN, candidate, review])
        result = enhanced_decision([{"role": "user", "content": "Tokyo and New York"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertEqual(result["message"], candidate)
        self.assertFalse(result["repair_attempted"])
        audit = result["review_evidence_audit"][0]
        self.assertEqual(audit["quote_status"], "quote_found")
        self.assertTrue(audit["subtask_index_valid"])
        self.assertFalse(audit["tool_known"])
        self.assertFalse(audit["call_index_valid"])

    def test_invalid_review_schema_is_reported_and_does_not_change_candidate(self):
        candidate = response(calls=[("get_time", '{"city":"Tokyo"}')])
        llm = ScriptedLLM([PLAN, candidate, response('{"schema_version":2,"needs_revision":true,"issues":["vague"]}')])
        result = enhanced_decision([{"role": "user", "content": "Tokyo"}], TOOLS,
                                   lambda messages, definitions, _purpose: llm.chat(messages, definitions))
        self.assertIsNotNone(result["review_error"])
        self.assertEqual(result["message"], candidate)
        self.assertFalse(result["repair_attempted"])

    def test_prompt_policy_hashes_are_versioned_and_stable(self):
        policy = prompt_policy()
        self.assertEqual(policy["version"], PROMPT_VERSION)
        self.assertEqual(policy["output_schema_version"], 2)
        self.assertEqual(set(policy["sha256"]), {"plan", "candidate", "review", "repair"})
        self.assertTrue(all(len(value) == 64 for value in policy["sha256"].values()))

    def test_missing_audit_detects_false_required_or_default_claims(self):
        tools = [{"type": "function", "function": {"name": "concert", "parameters": {
            "type": "object", "properties": {"artist": {"type": "string"},
            "year": {"type": "integer", "default": 2022}}, "required": ["artist"]}}}]
        plan = {"missing_information": [{"tool": "concert", "parameter": "year", "required": True,
                 "default_available": False, "blocks_action": True, "reason": "year missing"}]}
        audit = audit_missing_information(plan, tools)[0]
        self.assertFalse(audit["schema_required"])
        self.assertTrue(audit["schema_default_declared"])
        self.assertFalse(audit["required_claim_matches"])
        self.assertFalse(audit["default_claim_matches"])

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
            decision_event = next(event for event in events if event["type"] == "decision.finished")
            self.assertEqual(decision_event["payload"]["plan_evidence_audit"][0]["status"], "quote_found")
            self.assertTrue(decision_event["payload"]["repair_accepted"])
            self.assertEqual(decision_event["payload"]["prompt_policy"], prompt_policy())
            self.assertFalse(any(event["type"].startswith("tool.call.") for event in events))

    def test_bfcl_metadata_and_reference_answers_are_not_sent_to_model(self):
        case = FunctionCallCase("simple_python_0", "simple_python", [{"role": "user", "content": "Tokyo"}],
                                TOOLS, metadata={"reference_answer": "SECRET_GOLD_ANSWER"})
        llm = ScriptedLLM([PLAN, response(calls=[("get_time", '{"city":"Tokyo"}')]), REVIEW_OK])
        with tempfile.TemporaryDirectory() as temp:
            official, record = run_enhanced_case(case, llm, Path(temp))
        self.assertEqual(record["status"], "completed")
        self.assertNotIn("SECRET_GOLD_ANSWER", json.dumps(llm.requests))
        self.assertEqual(official["result"], [{"get_time": '{"city":"Tokyo"}'}])

    def test_real_agent_uses_same_enhanced_decision_before_executing_tool(self):
        llm = ScriptedLLM([PLAN, response(calls=[("finish", '{"answer":"完成"}')]), REVIEW_OK])
        recorder = EventRecorder()
        with redirect_stdout(io.StringIO()):
            result = agent.run("任务", llm, Memory("规则"), decision_mode="enhanced", event_sink=recorder)
        self.assertEqual(result["status"], "completed")
        purposes = [e["payload"]["purpose"] for e in recorder.events if e["type"] == "model.call.started"]
        self.assertEqual(purposes, ["decision.plan", "decision.candidate", "decision.review"])
        decision_event = next(e for e in recorder.events if e["type"] == "decision.finished")
        self.assertTrue(decision_event["payload"]["semantic_review_advisory_only"])
        self.assertEqual(decision_event["payload"]["prompt_policy"], prompt_policy())
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
            self.assertEqual(report["decision_prompt_policy"], prompt_policy())

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
                    "execution_path": "astrion_enhanced_decision", "case_count": manifest.case_count,
                    "decision_prompt_policy": prompt_policy(), "error_count": 0}))
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
            self.assertEqual(report["decision_prompt_policy"], prompt_policy())

    def test_parent_rejects_worker_with_different_prompt_version(self):
        from test_bfcl_astrion import FakeAstrionBFCLCommands
        fake = FakeAstrionBFCLCommands()
        def runner(command, env, cwd):
            result = fake(command, env, cwd)
            if command[1].endswith("bfcl_astrion.py"):
                path = cwd / GENERATION_REPORT
                payload = json.loads(path.read_text())
                payload["execution_path"] = "astrion_enhanced_decision"
                payload["decision_prompt_policy"] = {"version": "different"}
                path.write_text(json.dumps(payload))
            return result
        with tempfile.TemporaryDirectory() as temp:
            report = run_astrion_bfcl(api_model="deepseek-flash", bfcl_bin="/fake/bfcl", bfcl_python="/fake/python",
                                      manifest_path=BUILTIN_BENCHMARKS["trial-3"], output_dir=Path(temp) / "run",
                                      mode="enhanced", env={"DEEPSEEK_API_KEY": "test"}, command_runner=runner)
            self.assertEqual(report["status"], "orchestration_error")
            self.assertNotIn("evaluate", [command[1] for command in fake.calls])


if __name__ == "__main__":
    unittest.main()
