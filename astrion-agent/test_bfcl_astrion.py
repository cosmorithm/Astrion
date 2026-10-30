"""Astrion -> BFCL 单轮适配路径的离线测试；不访问模型或网络。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.bfcl import (
    BUILTIN_BENCHMARKS,
    DEFAULT_MODEL,
    PINNED_BFCL_VERSION,
    load_manifest,
    run_astrion_bfcl,
)
from benchmarks.bfcl_astrion import (
    GENERATION_REPORT,
    generate,
    normalize_bfcl_result,
    prepare_cases,
    run_case,
    validate_single_turn_manifest,
)
from benchmarks.common import FunctionCallCase


class FakeLLM:
    model = "fake-model"

    def __init__(self, response=None):
        self.response = response or {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
            }],
        }
        self.last_usage = {
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "total_tokens": 14,
        }
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append((messages, tools))
        return self.response


def command_result(command, *, stdout="", stderr="", returncode=0):
    return {
        "command": list(command),
        "returncode": returncode,
        "duration_seconds": 0.01,
        "stdout": stdout,
        "stderr": stderr,
    }


class FakeAstrionBFCLCommands:
    def __init__(self):
        self.calls = []
        self.seen_secret = False

    def __call__(self, command, env, cwd):
        self.calls.append(list(command))
        self.seen_secret = self.seen_secret or env.get("DEEPSEEK_API_KEY") == "secret"
        if command[1] == "version":
            return command_result(
                command, stdout=f"bfcl version: {PINNED_BFCL_VERSION}\n"
            )
        if command[1] == "models":
            return command_result(command, stdout=f"{DEFAULT_MODEL}\n")
        if command[1].endswith("bfcl_astrion.py"):
            manifest = load_manifest(BUILTIN_BENCHMARKS["trial-3"])
            result_dir = cwd / "result" / DEFAULT_MODEL
            result_dir.mkdir(parents=True, exist_ok=True)
            for category, ids in manifest.categories.items():
                path = result_dir / f"BFCL_v4_{category}_result.json"
                path.write_text("".join(
                    json.dumps({
                        "id": case_id,
                        "result": [],
                        "input_token_count": 5,
                        "output_token_count": 1,
                        "latency": 0.02,
                    }) + "\n"
                    for case_id in ids
                ), encoding="utf-8")
            (cwd / GENERATION_REPORT).write_text(json.dumps({
                "kind": "bfcl_astrion_generation",
                "status": "completed",
                "execution_path": "astrion_single_turn_adapter",
                "case_count": manifest.case_count,
                "error_count": 0,
            }), encoding="utf-8")
            return command_result(command, stdout="astrion generation complete\n")
        if command[1] == "evaluate":
            manifest = load_manifest(BUILTIN_BENCHMARKS["trial-3"])
            score_dir = cwd / "score" / DEFAULT_MODEL
            score_dir.mkdir(parents=True, exist_ok=True)
            for category, ids in manifest.categories.items():
                path = score_dir / f"BFCL_v4_{category}_score.json"
                path.write_text(json.dumps({
                    "accuracy": 1.0,
                    "correct_count": len(ids),
                    "total_count": len(ids),
                }) + "\n", encoding="utf-8")
            return command_result(command, stdout="evaluation complete\n")
        raise AssertionError(f"未预期命令: {command}")


class BFCLAstrionTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="astrion-bfcl-adapter-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def test_prepare_cases_keeps_manifest_order_and_converts_tools(self):
        manifest = load_manifest(BUILTIN_BENCHMARKS["trial-3"])

        def load(category):
            return [{
                "id": case_id,
                "question": [[{"role": "user", "content": case_id}]],
                "function": [{"name": f"tool_{case_id}"}],
            } for case_id in reversed(manifest.categories[category])]

        cases = prepare_cases(
            manifest,
            load,
            lambda functions: [{"type": "function", "function": functions[0]}],
        )

        self.assertEqual([case.id for case in cases], manifest.case_ids)
        self.assertEqual(cases[0].tools[0]["type"], "function")
        self.assertEqual(cases[0].metadata["function_count"], 1)

    def test_adapter_rejects_full_collection_and_missing_cases(self):
        full = load_manifest(BUILTIN_BENCHMARKS["all-scoring"])
        with self.assertRaisesRegex(ValueError, "all-scoring"):
            validate_single_turn_manifest(full)

        trial = load_manifest(BUILTIN_BENCHMARKS["trial-3"])
        with self.assertRaisesRegex(ValueError, "缺少样例"):
            prepare_cases(trial, lambda _category: [], lambda value: value)

    def test_normalize_supports_calls_and_irrelevance_content(self):
        calls = normalize_bfcl_result(FakeLLM().response)
        self.assertEqual(calls, [{"add": '{"a":1,"b":2}'}])
        self.assertEqual(
            normalize_bfcl_result({"role": "assistant", "content": "无需调用工具"}),
            "无需调用工具",
        )
        with self.assertRaisesRegex(ValueError, "JSON 字符串"):
            normalize_bfcl_result({"tool_calls": [{
                "function": {"name": "add", "arguments": {"a": 1}},
            }]})

    def test_run_case_records_event_v1_without_executing_tools(self):
        case = FunctionCallCase(
            id="simple_python_0",
            category="simple_python",
            messages=[{"role": "user", "content": "add"}],
            tools=[{"type": "function", "function": {"name": "add"}}],
        )
        llm = FakeLLM()

        official, record = run_case(case, llm, self.root / "events")

        self.assertEqual(official["result"], [{"add": '{"a":1,"b":2}'}])
        self.assertEqual(official["input_token_count"], 11)
        event_log = json.loads(Path(record["event_path"]).read_text(encoding="utf-8"))
        event_types = [item["type"] for item in event_log["events"]]
        self.assertEqual(event_types, [
            "run.started", "model.call.started", "model.call.finished", "run.finished",
        ])
        self.assertFalse(any(value.startswith("tool.call") for value in event_types))
        self.assertEqual(llm.requests[0][1], case.tools)

    def test_generate_writes_official_jsonl_and_aggregate_report(self):
        cases = [
            FunctionCallCase(
                id=f"{category}_0",
                category=category,
                messages=[{"role": "user", "content": "call"}],
                tools=[{"type": "function", "function": {"name": "add"}}],
            )
            for category in ("simple_python", "multiple")
        ]

        report = generate(
            cases,
            FakeLLM(),
            self.root,
            scorer_model=DEFAULT_MODEL,
            result_path=lambda root, model, category: (
                root / "result" / model / f"BFCL_v4_{category}_result.json"
            ),
        )

        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["completed_cases"], 2)
        self.assertEqual(report["token_usage"]["known_total_tokens"], 28)
        self.assertTrue((self.root / GENERATION_REPORT).is_file())
        self.assertEqual(len(list((self.root / "events").glob("*.json"))), 2)

    def test_parent_dry_run_creates_distinct_astrion_report_without_commands(self):
        fake = FakeAstrionBFCLCommands()
        report = run_astrion_bfcl(
            api_model="deepseek-chat",
            bfcl_bin="/fake/bfcl",
            bfcl_python="/fake/python",
            manifest_path=BUILTIN_BENCHMARKS["trial-3"],
            output_dir=self.root / "dry",
            dry_run=True,
            command_runner=fake,
        )

        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["kind"], "bfcl_astrion_run")
        self.assertEqual(report["execution_path"], "astrion_single_turn_adapter")
        self.assertFalse(report["config"]["executes_benchmark_tools"])
        self.assertEqual(fake.calls, [])

    def test_parent_runs_astrion_worker_then_official_scorer(self):
        fake = FakeAstrionBFCLCommands()
        report = run_astrion_bfcl(
            api_model="deepseek-chat",
            bfcl_bin="/fake/bfcl",
            bfcl_python="/fake/python",
            manifest_path=BUILTIN_BENCHMARKS["trial-3"],
            output_dir=self.root / "real",
            env={"DEEPSEEK_API_KEY": "secret"},
            command_runner=fake,
        )

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["official_outputs"]["scored_cases"], 3)
        self.assertEqual(report["official_outputs"]["derived_subset_micro_accuracy"], 1.0)
        self.assertEqual(report["generation_report"]["error_count"], 0)
        self.assertEqual(len(fake.calls), 4)
        self.assertTrue(fake.seen_secret)
        self.assertNotIn("secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
