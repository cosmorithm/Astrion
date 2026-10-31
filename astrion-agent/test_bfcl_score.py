"""BFCL 成绩查看脚本的离线测试。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import show_bfcl_score as score_viewer


class BFCLScoreViewerTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="astrion-agent-bfcl-score-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def make_run(self, run_id: str, *, status: str = "completed", score=0.75,
                 mtime: int = 1, kind: str = "bfcl_official_run") -> Path:
        run = self.root / run_id
        run.mkdir()
        outputs = None if status == "dry_run" else {
            "derived_subset_micro_accuracy": score,
            "correct_cases": 3,
            "scored_cases": 4,
            "official_category_scores": [{
                "category": "simple_python",
                "accuracy": 1.0,
                "correct_count": 1,
                "total_count": 1,
            }],
            "token_usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
            "model_latency_seconds": 1.25,
        }
        report = {
            "kind": kind,
            "status": status,
            "created_at": "2026-10-30T00:00:00+00:00",
            "benchmark": {"profile": "subset", "case_count": 4},
            "model": "DeepSeek-V3.2-Exp-FC",
            "commands": {
                "generate": {"duration_seconds": 2.0},
                "evaluate": {"duration_seconds": 0.5},
            },
            "official_outputs": outputs,
            "error": ({"type": "ExampleError", "message": "模拟失败"}
                      if status not in {"completed", "dry_run"} else None),
        }
        path = run / score_viewer.REPORT_NAME
        path.write_text(json.dumps(report), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_summarize_completed_report(self):
        path = self.make_run("run-a")
        summary = score_viewer.summarize_report(score_viewer.load_report(path), path)

        self.assertEqual(summary["run_id"], "run-a")
        self.assertEqual(summary["score"], 0.75)
        self.assertEqual(summary["token_usage"]["total_tokens"], 120)
        self.assertEqual(summary["command_total_seconds"], 2.5)
        self.assertEqual(summary["execution_path"], "official_model_handler")

    def test_accepts_and_labels_astrion_adapter_report(self):
        path = self.make_run("astrion", kind="bfcl_astrion_run")
        report = json.loads(path.read_text(encoding="utf-8"))
        report["execution_path"] = "astrion_single_turn_adapter"
        report["scorer_model"] = "DeepSeek-V3.2-Exp-FC"
        path.write_text(json.dumps(report), encoding="utf-8")

        summary = score_viewer.summarize_report(score_viewer.load_report(path), path)

        self.assertEqual(summary["execution_path"], "astrion_single_turn_adapter")
        self.assertEqual(summary["scorer_model"], "DeepSeek-V3.2-Exp-FC")
        self.assertIn(
            "执行路径：astrion_single_turn_adapter",
            score_viewer.format_summary(summary),
        )

    def test_displays_official_registry_and_effective_api_model_separately(self):
        path = self.make_run("flash-baseline")
        report = json.loads(path.read_text(encoding="utf-8"))
        report.update({
            "execution_path": "official_model_handler_api_override",
            "registry_model": "DeepSeek-V3.2-Exp-FC",
            "api_model": "deepseek-flash",
        })
        path.write_text(json.dumps(report), encoding="utf-8")

        summary = score_viewer.summarize_report(score_viewer.load_report(path), path)
        text = score_viewer.format_summary(summary)

        self.assertEqual(summary["model"], "DeepSeek-V3.2-Exp-FC")
        self.assertEqual(summary["api_model"], "deepseek-flash")
        self.assertIn("实际 API 模型：deepseek-flash", text)
        self.assertIn(
            "DeepSeek-V3.2-Exp-FC -> deepseek-flash",
            score_viewer.format_run_list([summary]),
        )

    def test_enhanced_report_displays_paired_score_and_partial_usage(self):
        path = self.make_run("enhanced", kind="bfcl_astrion_run")
        report = json.loads(path.read_text(encoding="utf-8"))
        report.update({
            "execution_path": "astrion_enhanced_decision",
            "decision_prompt_policy": {"version": "astrion-decision-v2.0"},
            "initial_official_outputs": {"derived_subset_micro_accuracy": 0.5, "correct_cases": 2, "scored_cases": 4},
            "paired_comparison": {"improved": 1, "regressed": 0, "both_correct": 2, "both_incorrect": 1},
            "generation_report": {"model_call_count": 13, "repair_count": 1, "effective_base_url": "https://example.test",
                "token_usage": {"known_input_tokens": 150, "known_output_tokens": 50,
                                "known_total_tokens": 200, "unknown_calls": 1}},
        })
        summary = score_viewer.summarize_report(report, path)
        text = score_viewer.format_summary(summary)
        self.assertIn("初始候选：50.00%（2/4）", text)
        self.assertIn("决策提示词版本：astrion-decision-v2.0", text)
        self.assertIn("改善 1 题，退步 0 题", text)
        self.assertIn("模型调用：13 次", text)
        self.assertIn("total 200", text)
        self.assertIn("1 次模型调用未返回完整 token", text)

    def test_latest_completed_skips_a_newer_failed_run(self):
        completed = self.make_run("completed", mtime=10)
        self.make_run("failed", status="generation_error", mtime=20)

        self.assertEqual(score_viewer.latest_completed_report(self.root), completed.resolve())

    def test_discover_runs_orders_newest_first_and_keeps_invalid(self):
        self.make_run("old", mtime=10)
        invalid = self.root / "new-invalid"
        invalid.mkdir()
        path = invalid / score_viewer.REPORT_NAME
        path.write_text("not json", encoding="utf-8")
        os.utime(path, (20, 20))

        runs = score_viewer.discover_runs(self.root)
        self.assertEqual([item["run_id"] for item in runs], ["new-invalid", "old"])
        self.assertEqual(runs[0]["status"], "invalid")

    def test_resolve_accepts_run_id_directory_and_file(self):
        path = self.make_run("chosen")

        self.assertEqual(score_viewer.resolve_report("chosen", self.root), path.resolve())
        self.assertEqual(score_viewer.resolve_report(str(path.parent), self.root), path.resolve())
        self.assertEqual(score_viewer.resolve_report(str(path), self.root), path.resolve())

    def test_text_output_contains_score_category_token_and_duration(self):
        path = self.make_run("readable")
        report = score_viewer.load_report(path)
        summary = score_viewer.summarize_report(report, path)
        text = score_viewer.format_summary(summary)

        self.assertIn("总分：75.00%（3/4）", text)
        self.assertIn("simple_python: 100.00%", text)
        self.assertIn("total 120", text)
        self.assertIn("合计 2.50 秒", text)

        report["commands"]["evaluate"] = {}
        partial = score_viewer.summarize_report(report, path)
        self.assertIn("generate 2.00 秒，合计 2.00 秒", score_viewer.format_summary(partial))

        report["benchmark"].update({
            "profile": "full_scoring",
            "case_count": 5217,
            "expected_scored_cases": 5106,
        })
        full = score_viewer.summarize_report(report, path)
        self.assertIn(
            "范围：full_scoring（生成条目 5217，预计计分 5106）",
            score_viewer.format_summary(full),
        )

    def test_json_cli_reads_a_selected_run(self):
        self.make_run("json-run")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = score_viewer.main([
                "--runs-dir", str(self.root), "--run", "json-run", "--json",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["score"], 0.75)

    def test_list_cli_shows_completed_and_failed_runs(self):
        self.make_run("good", mtime=10)
        self.make_run("bad", status="evaluation_error", mtime=20)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = score_viewer.main(["--runs-dir", str(self.root), "--list"])

        self.assertEqual(code, 0)
        self.assertIn("bad | evaluation_error", stdout.getvalue())
        self.assertIn("good | completed", stdout.getvalue())

    def test_missing_report_returns_a_clear_nonzero_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = score_viewer.main([
                "--runs-dir", str(self.root), "--run", "missing",
            ])

        self.assertEqual(code, 2)
        self.assertIn("没有找到 BFCL 报告", stderr.getvalue())

    def test_progress_cli_inspects_a_running_all_scoring_directory(self):
        run = self.root / "running-full"
        result = run / "result" / "model" / "agentic" / "memory" / "vector"
        result.mkdir(parents=True)
        path = result / "BFCL_v4_memory_vector_prereq_result.json"
        path.write_text(
            "\n".join(json.dumps({"id": f"memory_vector_prereq_{index}"})
                      for index in range(3)) + "\n",
            encoding="utf-8",
        )
        newer_dry = self.make_run("newer-dry", status="dry_run", mtime=50)
        os.utime(newer_dry.parent, (50, 50))
        os.utime(run, (10, 10))
        self.assertEqual(score_viewer.resolve_run_dir("", self.root), run.resolve())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = score_viewer.main([
                "--runs-dir", str(self.root), "--progress", "--run", "running-full",
            ])

        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("[BFCL进度] generation", text)
        self.assertIn("当前/最近组 19/22 memory_vector 3/192", text)
        self.assertIn("总条目 3/5217", text)
        self.assertIn("查看来源：filesystem_scan", text)
        self.assertIn("不能仅凭此判断进程仍在运行", text)


if __name__ == "__main__":
    unittest.main()
