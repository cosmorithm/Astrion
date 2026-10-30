"""基准测试适配层的离线测试：不访问网络，不判断任务对错。"""

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks import (
    FunctionCallCase,
    load_cases,
    run_case,
    run_cases,
    summarize,
    write_jsonl,
)


def make_case(case_id="case-1", category="simple", metadata=None):
    return FunctionCallCase(
        id=case_id,
        category=category,
        messages=[{"role": "user", "content": "测试问题"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        metadata=metadata or {},
    )


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="toy-agent-bench-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def test_case_validation(self):
        with self.assertRaises(ValueError):
            FunctionCallCase("", "simple", [{"role": "user"}], [])
        with self.assertRaises(ValueError):
            FunctionCallCase("id", "", [{"role": "user"}], [])
        with self.assertRaises(ValueError):
            FunctionCallCase("id", "simple", [], [])
        with self.assertRaises(ValueError):
            FunctionCallCase("id", "simple", [{"role": "user"}], None)
        with self.assertRaises(ValueError):
            FunctionCallCase("id", "simple", [{"role": "user"}], [], metadata=None)
        with self.assertRaises(ValueError):
            FunctionCallCase.from_dict({"category": "simple", "messages": [], "tools": []})

    def test_load_cases_and_reject_duplicate_ids(self):
        path = self.root / "cases.json"
        path.write_text(json.dumps([
            {"id": "a", "category": "simple",
             "messages": [{"role": "user", "content": "a"}], "tools": []},
            {"id": "b", "category": "parallel",
             "messages": [{"role": "user", "content": "b"}], "tools": []},
        ], ensure_ascii=False), encoding="utf-8")

        cases = load_cases(path)
        self.assertEqual([case.id for case in cases], ["a", "b"])
        self.assertEqual(cases[1].category, "parallel")

        path.write_text(json.dumps([
            {"id": "a", "category": "simple", "messages": [{"role": "user"}], "tools": []},
            {"id": "a", "category": "simple", "messages": [{"role": "user"}], "tools": []},
        ]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_cases(path)

    def test_run_case_isolates_request_and_records_raw_response(self):
        case = make_case(metadata={"source": "unit"})
        seen = []

        def chat(messages, tools):
            seen.append((messages, tools))
            messages[0]["content"] = "被模型侧改写"
            tools.clear()
            return {"role": "assistant", "content": "ok"}

        record = run_case(case, chat, model="test-model")

        self.assertEqual(case.messages[0]["content"], "测试问题")
        self.assertEqual(len(case.tools), 1)
        self.assertEqual(seen[0][0][0]["content"], "被模型侧改写")
        self.assertEqual(record["messages"][0]["content"], "测试问题")
        self.assertEqual(len(record["tools"]), 1)
        self.assertEqual(record["response"], {"role": "assistant", "content": "ok"})
        self.assertEqual(record["model"], "test-model")
        self.assertEqual(record["metadata"], {"source": "unit"})
        self.assertIsInstance(record["timestamp"], str)

    def test_run_cases_limit_and_error_isolation(self):
        cases = [make_case(f"case-{index}") for index in range(3)]
        calls = {"count": 0}

        def flaky(messages, tools):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("模拟单条失败")
            return {"role": "assistant", "content": "ok"}

        records = run_cases(cases, flaky, limit=2, model="test-model")

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["id"], "case-0")
        self.assertEqual(records[1]["error"]["type"], "RuntimeError")
        self.assertEqual(records[1]["category"], "simple")

        def always_fail(messages, tools):
            raise RuntimeError("总是失败")

        with self.assertRaises(RuntimeError):
            run_cases(cases, always_fail, limit=1, continue_on_error=False)

        with self.assertRaises(ValueError):
            run_cases(cases, flaky, limit=-1)

    def test_write_jsonl_and_summarize(self):
        records = [
            {"id": "a", "category": "simple", "response": {"content": "ok"}},
            {"id": "b", "category": "simple", "error": {"type": "RuntimeError"}},
            {"id": "c", "category": "parallel", "response": {"content": "ok"}},
        ]
        path = write_jsonl(records, self.root / "runs" / "results.jsonl")

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["id"], "a")

        summary = summarize(records)
        self.assertEqual(summary["total_cases"], 3)
        self.assertEqual(summary["categories"], {"simple": 2, "parallel": 1})
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["error_cases"], ["b"])

    def test_load_cases_rejects_a_non_list_file(self):
        path = self.root / "cases.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_cases(path)


if __name__ == "__main__":
    unittest.main()
