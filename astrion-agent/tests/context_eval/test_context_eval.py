import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.context_counter import CodepointCounter

from evaluation.context.runner import (
    grade,
    initial_history,
    load_cases,
    new_report,
    run_group,
    save_report,
    summarize,
)
from evaluation.context.rescore import rescore
from evaluation.context.report import render

CASES = Path(__file__).resolve().parents[2] / "evaluation/context/cases.json"


class FakeModel:
    model = "fake"
    last_usage = None

    def __init__(self):
        self.prompts = []

    def chat(self, messages, tools=None):
        self.prompts.append(messages)
        self.last_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
        if messages[0]["content"].startswith("You are summarizing"):
            data = json.loads(messages[1]["content"])
            return {"content": json.dumps({"summary": "step " + data["step_id"]})}
        if messages[0]["content"].startswith("You select past"):
            return {"content": json.dumps({"selections": []})}
        return {"content": json.dumps({"answer": "1350"})}

    def summarize(self, transcript):
        self.last_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }
        return json.dumps({"progress": "Done", "pending": "Answer", "evidence_ids": []})


class ContextEvalTests(unittest.TestCase):
    def setUp(self):
        token_patch = patch(
            "context_construction.load_token_counter", return_value=CodepointCounter()
        )
        token_patch.start()
        self.addCleanup(token_patch.stop)
        runner_patch = patch(
            "evaluation.context.runner.load_token_counter",
            return_value=CodepointCounter(),
        )
        runner_patch.start()
        self.addCleanup(runner_patch.stop)

    def test_cases_validate_and_are_distinct(self):
        data = load_cases(CASES)
        self.assertEqual(len(data["cases"]), 6)
        self.assertEqual(len({c["id"] for c in data["cases"]}), 6)

    def test_strict_grade(self):
        self.assertTrue(
            grade({"content": '```json\n{"answer":" 1350 "}\n```'}, ["1350"])["correct"]
        )
        self.assertFalse(grade({"content": "1350"}, ["1350"])["correct"])
        numeric = grade({"content": '{"answer":1350}'}, ["1350"])
        self.assertTrue(numeric["correct"])
        self.assertFalse(numeric["format_compliant"])
        self.assertTrue(grade({"content": '{"answer":"1350.0"}'}, ["1350"])["correct"])
        self.assertFalse(grade({"content": '{"answer":true}'}, ["1350"])["correct"])
        self.assertFalse(
            grade({"content": '{"answer":"1350 mg"}'}, ["1350"])["correct"]
        )

    def test_paired_inputs_answers_are_not_leaked_and_report_incremental(self):
        case = next(c for c in load_cases(CASES)["cases"] if c["id"] == "C03")
        rows = []
        for mode in ("append", "legacy", "ss"):
            model = FakeModel()
            row = run_group(case, mode, model)
            rows.append(row)
            self.assertEqual(row["status"], "completed")
            self.assertTrue(row["score"]["correct"])
            # Correct answer 1350 is not in any source or question sent to model.
            self.assertNotIn("1350", json.dumps(model.prompts, ensure_ascii=False))
            self.assertEqual(row["construction"]["message_protocol_errors"], [])
            self.assertGreater(row["usage"]["known_totals"]["total_tokens"], 0)
        self.assertEqual(len({r["construction"]["initial_chars"] for r in rows}), 1)
        self.assertEqual(rows[2]["construction"]["status"], "applied")
        self.assertEqual(len(rows[2]["construction"]["ss_catalog"]), 8)
        self.assertEqual(summarize(rows)["ss"]["answer_correct"], 1)
        report = new_report(CASES, [case], "fake", ("append", "legacy", "ss"))
        report["groups"] = rows
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            save_report(report, path)
            self.assertEqual(len(json.loads(path.read_text())["groups"]), 3)

    def test_report_and_rescore_keep_the_old_score(self):
        case = load_cases(CASES)["cases"][0]
        report = new_report(CASES, [case], "fake", ("append",))
        report["groups"] = [
            {
                "case_id": case["id"],
                "mode": "append",
                "status": "completed",
                "construction": {
                    "status": "applied",
                    "within_target": False,
                    "initial_chars": 10,
                    "after_chars": 10,
                    "target_chars": 5,
                },
                "score": {"status": "invalid_answer_schema", "correct": False},
                "answer_response": {"content": '{"answer":122}'},
                "usage": {"known_totals": {"total_tokens": 1}, "complete": True},
                "calls": [{"purpose": "answer", "usage": {"total_tokens": 1}}],
                "duration_seconds": 1,
            }
        ]
        rescored = rescore(report, [case])
        self.assertTrue(rescored["groups"][0]["score"]["correct"])
        self.assertFalse(rescored["groups"][0]["score"]["format_compliant"])
        self.assertFalse(rescored["groups"][0]["score_before_rescore"]["correct"])
        text = render(rescored, [case])
        self.assertIn("不适用", text)
        self.assertIn("122", text)

    def test_hidden_transport_retry_is_marked_as_unknown_usage(self):
        case = load_cases(CASES)["cases"][0]

        class Retried(FakeModel):
            def chat(self, messages, tools=None):
                print("[模型] 请求失败，随后重试")
                return super().chat(messages, tools)

        row = run_group(case, "append", Retried())
        self.assertFalse(row["usage"]["complete"])
        self.assertEqual(row["usage"]["unknown_calls"], 1)
        self.assertEqual(row["calls"][0]["transport_retry_count"], 1)

    def test_multiple_followups_reuse_one_build_and_respect_budget(self):
        case = next(c for c in load_cases(CASES)["cases"] if c["id"] == "C03")
        model = FakeModel()
        followups = [
            {"question": "再核对一次", "accepted": ["1350"]},
            {"question": "再核对第二次", "accepted": ["1350"]},
        ]
        row = run_group(case, "ss", model, followups=followups)
        self.assertEqual(row["construction"]["status"], "applied")
        self.assertEqual(len(row["followups"]), 2)
        self.assertTrue(all(x["score"]["correct"] for x in row["followups"]))
        self.assertEqual(sum(c["purpose"] == "context_plan" for c in row["calls"]), 1)
        self.assertTrue(
            all(
                c["local_input_tokens"] <= row["construction"]["input_budget_tokens"]
                for c in row["calls"]
                if c["sent"]
            )
        )
        self.assertLessEqual(
            row["construction"]["after_local_tokens"],
            row["construction"]["target_tokens"],
        )

    def test_failed_summary_is_not_claimed_as_success(self):
        case = load_cases(CASES)["cases"][0]

        class Failing(FakeModel):
            def chat(self, messages, tools=None):
                if messages[0]["content"].startswith("You are summarizing"):
                    raise RuntimeError("failure")
                return super().chat(messages, tools)

        row = run_group(case, "ss", Failing())
        self.assertEqual(row["construction"]["status"], "failed")
        self.assertFalse(row["usage"]["complete"])
        self.assertEqual(row["construction"]["gold_step_selection_recall"], None)


if __name__ == "__main__":
    unittest.main()
