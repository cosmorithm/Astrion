"""评测总报告的统计口径、持久化和错误处理测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation import summarize_suite


def case_report(case_id, statuses, *, run_status="completed", report_path="/tmp/case.json"):
    return {
        "case_id": case_id,
        "run": {"result": {"status": run_status}, "duration_seconds": 1.25},
        "judgments": [
            {"judge": f"judge_{index}", "status": status, "reason": f"结果 {status}",
             "evidence": {"index": index}}
            for index, status in enumerate(statuses)
        ],
        "report_path": report_path,
        "report_error": None,
    }


class EvaluationSummaryTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="astrion-agent-suite-tests-")
        self.addCleanup(storage.cleanup)
        self.report_dir = (Path(storage.name) / "reports").resolve()
        directory_patch = patch("evaluation.reporting.DEFAULT_REPORT_DIR", self.report_dir)
        directory_patch.start()
        self.addCleanup(directory_patch.stop)

    def test_mixed_results_show_both_denominators_and_save_summary(self):
        reports = [case_report("passed", ["pass"]),
                   case_report("failed", ["fail"]),
                   case_report("unknown", ["unknown"], run_status="setup_error")]

        summary = summarize_suite(reports)

        self.assertEqual(summary["total_cases"], 3)
        self.assertEqual((summary["passed_cases"], summary["failed_cases"], summary["unresolved_cases"]), (1, 1, 1))
        self.assertEqual(summary["pass_rate_percent"], 33.33)
        self.assertEqual(summary["scored_pass_rate_percent"], 50.0)
        self.assertEqual(summary["run_status_counts"], {"completed": 2, "setup_error": 1})
        self.assertEqual(summary["run_error_cases"], 1)
        self.assertEqual([item["outcome"] for item in summary["cases"]], ["pass", "fail", "unresolved"])
        path = Path(summary["report_path"])
        self.assertEqual(path.parent, self.report_dir)
        self.assertTrue(path.name.startswith("suite_"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), summary)

    def test_multiple_judges_require_all_pass_and_preserve_incomplete_states(self):
        reports = [
            case_report("all_pass", ["pass", "pass"]),
            case_report("one_fail", ["pass", "fail"]),
            case_report("fail_and_error", ["fail", "error"]),
            case_report("pass_and_unknown", ["pass", "unknown"]),
            case_report("pending", ["pending"]),
            case_report("error", ["error"]),
            case_report("no_judges", []),
        ]

        summary = summarize_suite(reports)

        self.assertEqual([item["outcome"] for item in summary["cases"]],
                         ["pass", "fail", "fail", "unresolved", "unresolved", "unresolved", "unresolved"])
        self.assertEqual((summary["passed_cases"], summary["failed_cases"], summary["unresolved_cases"]), (1, 2, 4))
        self.assertEqual(summary["pass_rate_percent"], 14.29)
        self.assertEqual(summary["scored_pass_rate_percent"], 33.33)

    def test_all_unresolved_have_no_scored_pass_rate(self):
        summary = summarize_suite([case_report("unknown", ["unknown"]), case_report("error", ["error"])])

        self.assertEqual(summary["pass_rate_percent"], 0.0)
        self.assertIsNone(summary["scored_pass_rate_percent"])
        self.assertEqual(summary["unresolved_cases"], 2)

    def test_empty_suite_has_no_rates(self):
        summary = summarize_suite([])

        self.assertEqual(summary["total_cases"], 0)
        self.assertIsNone(summary["pass_rate_percent"])
        self.assertIsNone(summary["scored_pass_rate_percent"])
        self.assertEqual(summary["cases"], [])

    def test_run_status_is_reported_separately_from_judge_outcome(self):
        reports = [case_report("partial", ["pass"], run_status="step_limit"),
                   case_report("error", ["pass"], run_status="error")]

        summary = summarize_suite(reports)

        self.assertEqual(summary["passed_cases"], 2)
        self.assertEqual(summary["run_status_counts"], {"step_limit": 1, "error": 1})
        self.assertEqual(summary["run_error_cases"], 1)
        self.assertEqual(summary["cases"][0]["run_status"], "step_limit")
        self.assertEqual(summary["cases"][1]["run_status"], "error")

    def test_missing_case_report_is_visible_in_summary(self):
        report = case_report("not_saved", ["pass"], report_path=None)
        report["report_error"] = {"type": "OSError", "message": "磁盘不可用"}

        summary = summarize_suite([report])

        self.assertEqual(summary["saved_case_reports"], 0)
        self.assertIsNone(summary["cases"][0]["report_path"])
        self.assertEqual(summary["cases"][0]["report_error"]["message"], "磁盘不可用")

    def test_summary_save_failure_preserves_counts_and_case_results(self):
        with patch("evaluation.suite.save_report", side_effect=OSError("磁盘不可用")):
            summary = summarize_suite([case_report("passed", ["pass"])])

        self.assertEqual(summary["passed_cases"], 1)
        self.assertEqual(summary["pass_rate_percent"], 100.0)
        self.assertIsNone(summary["report_path"])
        self.assertEqual(summary["report_error"], {"type": "OSError", "message": "磁盘不可用"})

    def test_summary_is_independent_from_later_changes_to_original_report(self):
        report = case_report("passed", ["pass"])
        summary = summarize_suite([report])
        report["judgments"][0]["evidence"]["index"] = 99

        self.assertEqual(summary["cases"][0]["judgments"][0]["evidence"]["index"], 0)
        self.assertEqual(json.loads(Path(summary["report_path"]).read_text(encoding="utf-8")), summary)

    def test_repeated_summaries_do_not_overwrite_previous_report(self):
        first = summarize_suite([case_report("first", ["pass"])])
        second = summarize_suite([case_report("second", ["fail"])])

        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertEqual(json.loads(Path(first["report_path"]).read_text(encoding="utf-8")), first)
        self.assertEqual(json.loads(Path(second["report_path"]).read_text(encoding="utf-8")), second)


if __name__ == "__main__":
    unittest.main()
