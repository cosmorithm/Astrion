"""BFCL 官方旁路适配层的离线测试；不安装 BFCL、不访问网络。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.bfcl import (
    BUILTIN_BENCHMARKS,
    DEFAULT_BENCHMARK,
    DEFAULT_MANIFEST,
    PINNED_BFCL_VERSION,
    _bfcl_python,
    _parse_version,
    audit_resume_state,
    build_progress_snapshot,
    build_commands,
    collect_official_outputs,
    format_progress,
    load_bfcl_env,
    load_manifest,
    official_id_payload,
    probe_bfcl,
    repair_resume_state,
    resume_bfcl,
    run_bfcl,
    write_official_id_file,
)


MODEL = "DeepSeek-V3.2-Exp-FC"


def command_result(command, *, returncode=0, stdout="", stderr="", duration=0.01):
    return {
        "command": list(command),
        "returncode": returncode,
        "duration_seconds": duration,
        "stdout": stdout,
        "stderr": stderr,
    }


class FakeBFCL:
    def __init__(self, *, generation_returncode=0, evaluation_returncode=0):
        self.calls = []
        self.generation_returncode = generation_returncode
        self.evaluation_returncode = evaluation_returncode
        self.seen_roots = []
        self.seen_openai_keys = []

    def __call__(self, command, env, cwd):
        self.calls.append(list(command))
        self.seen_roots.append(env.get("BFCL_PROJECT_ROOT"))
        self.seen_openai_keys.append(env.get("OPENAI_API_KEY"))
        action = command[1]
        if action == "version":
            return command_result(command, stdout=f"bfcl version: {PINNED_BFCL_VERSION}\n")
        if action == "models":
            return command_result(command, stdout=f"gorilla-openfunctions-v2\n{MODEL}\n")
        if action == "generate":
            if self.generation_returncode:
                return command_result(command, returncode=self.generation_returncode,
                                      stderr="模拟 generation 失败")
            result_dir = cwd / "result" / MODEL
            result_dir.mkdir(parents=True, exist_ok=True)
            for category in load_manifest().categories:
                path = result_dir / f"BFCL_v4_{category}_result.json"
                ids = load_manifest().categories[category]
                path.write_text("".join(
                    json.dumps({
                        "id": case_id,
                        "result": [],
                        "input_token_count": 10,
                        "output_token_count": 2,
                        "latency": 0.1,
                    }) + "\n"
                    for case_id in ids
                ), encoding="utf-8")
            return command_result(command, stdout="generation complete\n")
        if action == "evaluate":
            if self.evaluation_returncode:
                return command_result(command, returncode=self.evaluation_returncode,
                                      stderr="模拟 evaluation 失败")
            score_dir = cwd / "score" / MODEL
            score_dir.mkdir(parents=True, exist_ok=True)
            for index, (category, ids) in enumerate(load_manifest().categories.items()):
                correct = len(ids) if index != 1 else len(ids) - 1
                records = [{
                    "accuracy": correct / len(ids),
                    "correct_count": correct,
                    "total_count": len(ids),
                }]
                if correct != len(ids):
                    records.append({"id": ids[-1], "valid": False, "error": "模拟错误"})
                path = score_dir / f"BFCL_v4_{category}_score.json"
                path.write_text("".join(json.dumps(item) + "\n" for item in records),
                                encoding="utf-8")
            (cwd / "score" / "data_overall.csv").write_text(
                "Model,Overall Acc\nmodel,N/A\n", encoding="utf-8"
            )
            return command_result(command, stdout="evaluation complete\n")
        raise AssertionError(f"未预期命令: {command}")


class ResumeFakeBFCL:
    """只补 trial-3 缺失结果，模拟官方 harness 的 existing-id 跳过语义。"""

    def __init__(self):
        self.calls = []

    def __call__(self, command, env, cwd):
        self.calls.append(list(command))
        action = command[1]
        if action == "version":
            return command_result(command, stdout=f"bfcl version: {PINNED_BFCL_VERSION}\n")
        if action == "models":
            return command_result(command, stdout=f"{MODEL}\n")
        if action == "generate":
            result_dir = cwd / "result" / MODEL
            simple_path = result_dir / "BFCL_v4_simple_python_result.json"
            self.assert_existing_simple(simple_path)
            for category, case_id in (
                ("multiple", "multiple_0"),
                ("irrelevance", "irrelevance_0"),
            ):
                path = result_dir / f"BFCL_v4_{category}_result.json"
                path.write_text(json.dumps({
                    "id": case_id,
                    "result": [],
                    "input_token_count": 5,
                    "output_token_count": 1,
                    "latency": 0.05,
                }) + "\n", encoding="utf-8")
            return command_result(command, stdout="resumed generation complete\n")
        if action == "evaluate":
            score_dir = cwd / "score" / MODEL
            score_dir.mkdir(parents=True, exist_ok=True)
            for category in ("simple_python", "multiple", "irrelevance"):
                (score_dir / f"BFCL_v4_{category}_score.json").write_text(
                    json.dumps({
                        "accuracy": 1.0,
                        "correct_count": 1,
                        "total_count": 1,
                    }) + "\n",
                    encoding="utf-8",
                )
            return command_result(command, stdout="resumed evaluation complete\n")
        raise AssertionError(f"未预期命令: {command}")

    @staticmethod
    def assert_existing_simple(path):
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
        if [item["id"] for item in records] != ["simple_python_0"]:
            raise AssertionError("续跑不应覆盖已成功的 simple_python 结果")


class BFCLTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="toy-agent-bfcl-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def test_default_manifest_is_a_fixed_two_hundred_case_subset(self):
        manifest = load_manifest()

        self.assertEqual(DEFAULT_BENCHMARK, "baseline-200")
        self.assertEqual(set(BUILTIN_BENCHMARKS), {
            "baseline-200", "smoke-20", "trial-3", "all-scoring",
        })
        self.assertEqual(manifest.path, DEFAULT_MANIFEST.resolve())
        self.assertEqual(manifest.harness_version, PINNED_BFCL_VERSION)
        self.assertEqual(manifest.benchmark_version, "V4")
        self.assertEqual(manifest.profile, "subset")
        self.assertEqual(len(manifest.categories), 5)
        self.assertTrue(all(len(ids) == 40 for ids in manifest.categories.values()))
        self.assertEqual(len(manifest.case_ids), 200)
        self.assertEqual(len(set(manifest.case_ids)), 200)
        self.assertTrue(manifest.uses_run_ids)
        self.assertEqual(manifest.case_count, 200)
        self.assertEqual(manifest.expected_scored_cases, 200)

        smoke = load_manifest(
            DEFAULT_MANIFEST.with_name("bfcl_v4_single_turn_smoke.json")
        )
        self.assertEqual(len(smoke.case_ids), 20)

        trial = load_manifest(
            DEFAULT_MANIFEST.with_name("bfcl_v4_single_turn_trial_3.json")
        )
        self.assertEqual(len(trial.case_ids), 3)
        self.assertEqual(
            trial.case_ids,
            ["simple_python_0", "multiple_0", "irrelevance_0"],
        )

        full = load_manifest(
            DEFAULT_MANIFEST.with_name("bfcl_v4_all_scoring_full.json")
        )
        self.assertFalse(full.uses_run_ids)
        self.assertEqual(full.official_collection, "all_scoring")
        self.assertEqual(full.case_count, 5217)
        self.assertEqual(full.expected_scored_cases, 5106)
        self.assertEqual(len(full.category_counts), 22)
        self.assertEqual(sum(full.category_counts.values()), 5217)
        self.assertEqual(full.case_ids, [])

    def test_manifest_rejects_duplicate_and_mismatched_ids(self):
        data = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        data["categories"]["multiple"][0] = "simple_python_0"
        path = self.root / "bad.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError):
            load_manifest(path)

        data = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        data["categories"]["multiple"][0] = data["categories"]["multiple"][1]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_manifest(path)

    def test_manifest_rejects_an_unpinned_harness_version(self):
        data = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        data["harness"]["version"] = "latest"
        path = self.root / "wrong-version.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError):
            load_manifest(path)

        full_data = json.loads(
            BUILTIN_BENCHMARKS["all-scoring"].read_text(encoding="utf-8")
        )
        full_data["selection"]["expected_generation_entries"] = 5216
        path.write_text(json.dumps(full_data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_manifest(path)

        full_data["selection"]["expected_generation_entries"] = 5217
        full_data["selection"]["collection"] = "all"
        path.write_text(json.dumps(full_data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_manifest(path)

    def test_official_id_file_contains_only_the_bfcl_mapping(self):
        manifest = load_manifest()
        path = write_official_id_file(manifest, self.root)

        self.assertEqual(path, (self.root / "test_case_ids_to_generate.json").resolve())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         official_id_payload(manifest))
        self.assertNotIn("harness", json.loads(path.read_text(encoding="utf-8")))

        full = load_manifest(DEFAULT_MANIFEST.with_name("bfcl_v4_all_scoring_full.json"))
        with self.assertRaises(ValueError):
            official_id_payload(full)

    def test_commands_use_official_generate_then_partial_evaluate(self):
        manifest = load_manifest()
        generate, evaluate = build_commands(
            "/venv/bin/bfcl", MODEL, manifest, num_threads=2,
            temperature=0.001, allow_overwrite=True,
        )

        self.assertEqual(generate[:4], ["/venv/bin/bfcl", "generate", "--model", MODEL])
        self.assertIn("--run-ids", generate)
        self.assertIn("--allow-overwrite", generate)
        self.assertEqual(generate[generate.index("--num-threads") + 1], "2")
        self.assertEqual(evaluate[:4], ["/venv/bin/bfcl", "evaluate", "--model", MODEL])
        self.assertIn("--partial-eval", evaluate)
        categories = evaluate[evaluate.index("--test-category") + 1].split(",")
        self.assertEqual(categories, list(manifest.categories))

        full = load_manifest(DEFAULT_MANIFEST.with_name("bfcl_v4_all_scoring_full.json"))
        full_generate, full_evaluate = build_commands("bfcl", MODEL, full)
        self.assertNotIn("--run-ids", full_generate)
        self.assertEqual(
            full_generate[full_generate.index("--test-category") + 1], "all_scoring"
        )
        self.assertEqual(
            full_evaluate[full_evaluate.index("--test-category") + 1], "all_scoring"
        )
        self.assertNotIn("--partial-eval", full_evaluate)

    def test_doctor_checks_version_model_and_credential_without_model_call(self):
        project = self.root / "project"
        child = project / "toy-agent"
        child.mkdir(parents=True)
        (project / ".env").write_text(
            "DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            loaded = load_bfcl_env(child)
            self.assertEqual(loaded, [(project / ".env").resolve()])
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "from-dotenv")
            os.environ["DEEPSEEK_API_KEY"] = "from-shell"
            load_bfcl_env(child)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "from-shell")

        fake = FakeBFCL()
        result = probe_bfcl(
            "bfcl", MODEL, env={"DEEPSEEK_API_KEY": "secret"},
            cwd=self.root, credential_env="DEEPSEEK_API_KEY", command_runner=fake,
        )

        self.assertTrue(result["ok"])
        self.assertEqual([call[1] for call in fake.calls], ["version", "models"])
        credential = next(item for item in result["checks"] if item["name"] == "credential")
        self.assertEqual(credential["actual"], "present")
        self.assertNotIn("secret", json.dumps(result))

    def test_doctor_rejects_wrong_version_missing_model_and_missing_key(self):
        self.assertIsNone(
            _parse_version("Traceback source: bfcl version: {_version('bfcl')}")
        )

        def broken(command, env, cwd):
            if command[1] == "version":
                return command_result(command, stdout="bfcl version: 1.0\n")
            return command_result(command, stdout="another-model\n")

        result = probe_bfcl(
            "bfcl", MODEL, env={}, cwd=self.root,
            credential_env="DEEPSEEK_API_KEY", command_runner=broken,
        )

        self.assertFalse(result["ok"])
        self.assertEqual([item["ok"] for item in result["checks"]], [False, False, False])

    def test_doctor_reports_a_missing_executable_without_traceback(self):
        bin_dir = self.root / "venv with spaces" / "bin"
        bin_dir.mkdir(parents=True)
        launcher = bin_dir / "bfcl"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        python = bin_dir / "python"
        python.write_text("", encoding="utf-8")
        self.assertEqual(_bfcl_python(str(launcher)), str(python.resolve()))

        def missing(command, env, cwd):
            raise FileNotFoundError("bfcl 不存在")

        result = probe_bfcl(
            "missing-bfcl", MODEL, env={}, cwd=self.root,
            command_runner=missing,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"][0]["command_returncode"], None)
        self.assertEqual(result["checks"][1]["command_returncode"], None)

    def test_full_orchestration_imports_official_scores_and_preserves_logs(self):
        fake = FakeBFCL()
        output = self.root / "run"
        report = run_bfcl(
            model=MODEL,
            bfcl_bin="bfcl",
            output_dir=output,
            credential_env="DEEPSEEK_API_KEY",
            env={"DEEPSEEK_API_KEY": "do-not-record"},
            command_runner=fake,
        )

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["benchmark"]["case_count"], 200)
        official = report["official_outputs"]
        self.assertEqual(len(official["official_category_scores"]), 5)
        self.assertEqual(official["correct_cases"], 199)
        self.assertEqual(official["scored_cases"], 200)
        self.assertEqual(official["derived_subset_micro_accuracy"], 0.995)
        self.assertEqual(official["token_usage"], {
            "input_tokens": 2000,
            "output_tokens": 400,
            "total_tokens": 2400,
        })
        self.assertAlmostEqual(official["model_latency_seconds"], 20.0)
        self.assertEqual(sum(item["record_count"] for item in official["result_files"]), 200)
        self.assertTrue((output / report["commands"]["generate"]["stdout_log"]).is_file())
        self.assertTrue((output / report["commands"]["evaluate"]["stdout_log"]).is_file())
        saved = json.loads((output / "bfcl_run_report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, report)
        self.assertNotIn("do-not-record", json.dumps(saved))
        self.assertTrue(all(Path(root) == output.resolve() for root in fake.seen_roots))
        self.assertTrue(all(key == "do-not-record" for key in fake.seen_openai_keys))
        self.assertTrue(report["config"]["deepseek_openai_bootstrap_alias"])
        progress = json.loads((output / "bfcl_progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["phase"], "completed")
        self.assertEqual(progress["generation"]["completed_entries"], 200)
        self.assertEqual(progress["completed_generation_groups"], 5)
        self.assertEqual(progress["scored_groups"], 5)
        self.assertIn("总条目 200/200", format_progress(progress))
        self.assertEqual(
            build_progress_snapshot(output, load_manifest(), "completed")["generation"],
            progress["generation"],
        )

    def test_generation_failure_is_saved_and_evaluation_is_skipped(self):
        fake = FakeBFCL(generation_returncode=7)
        output = self.root / "generation-failed"

        report = run_bfcl(
            model=MODEL, bfcl_bin="bfcl", output_dir=output,
            env={"DEEPSEEK_API_KEY": "secret"}, credential_env="DEEPSEEK_API_KEY",
            command_runner=fake,
        )

        self.assertEqual(report["status"], "generation_error")
        self.assertEqual([call[1] for call in fake.calls], ["version", "models", "generate"])
        self.assertEqual(json.loads((output / "bfcl_run_report.json").read_text()), report)
        progress = json.loads((output / "bfcl_progress.json").read_text())
        self.assertEqual(progress["phase"], "generation_error")
        self.assertEqual(progress["generation"]["completed_entries"], 0)

        interrupted_fake = FakeBFCL()
        interrupted_output = self.root / "interrupted"

        def interrupted(command, env, cwd):
            if command[1] == "generate":
                raise KeyboardInterrupt
            return interrupted_fake(command, env, cwd)

        interrupted_report = run_bfcl(
            model=MODEL,
            bfcl_bin="bfcl",
            output_dir=interrupted_output,
            env={"DEEPSEEK_API_KEY": "secret"},
            credential_env="DEEPSEEK_API_KEY",
            command_runner=interrupted,
        )
        self.assertEqual(interrupted_report["status"], "interrupted")
        self.assertEqual(interrupted_report["error"]["type"], "KeyboardInterrupt")
        self.assertEqual(
            json.loads((interrupted_output / "bfcl_progress.json").read_text())["phase"],
            "interrupted",
        )

    def test_evaluation_failure_preserves_generated_results_and_report(self):
        fake = FakeBFCL(evaluation_returncode=9)
        output = self.root / "evaluation-failed"

        report = run_bfcl(
            model=MODEL, bfcl_bin="bfcl", output_dir=output,
            env={"DEEPSEEK_API_KEY": "secret"}, credential_env="DEEPSEEK_API_KEY",
            command_runner=fake,
        )

        self.assertEqual(report["status"], "evaluation_error")
        self.assertEqual(len(report["official_outputs"]["result_files"]), 5)
        self.assertEqual(report["official_outputs"]["official_category_scores"], [])
        self.assertEqual(json.loads((output / "bfcl_run_report.json").read_text()), report)

    def test_dry_run_materializes_commands_without_requiring_bfcl_or_key(self):
        calls = []

        def must_not_run(command, env, cwd):
            calls.append(command)
            raise AssertionError("dry-run 不应执行命令")

        output = self.root / "dry-run"
        report = run_bfcl(
            model=MODEL, bfcl_bin="missing-bfcl", output_dir=output,
            dry_run=True, command_runner=must_not_run,
        )

        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(calls, [])
        self.assertTrue((output / "test_case_ids_to_generate.json").is_file())
        self.assertEqual(json.loads((output / "bfcl_run_report.json").read_text()), report)
        self.assertEqual(
            json.loads((output / "bfcl_progress.json").read_text())["phase"],
            "dry_run",
        )

        full_output = self.root / "full-dry-run"
        full_report = run_bfcl(
            model=MODEL,
            bfcl_bin="missing-bfcl",
            manifest_path=DEFAULT_MANIFEST.with_name("bfcl_v4_all_scoring_full.json"),
            output_dir=full_output,
            dry_run=True,
            command_runner=must_not_run,
        )
        self.assertEqual(full_report["benchmark"]["case_count"], 5217)
        self.assertEqual(full_report["benchmark"]["expected_scored_cases"], 5106)
        self.assertFalse(full_report["benchmark"]["subset"])
        self.assertIsNone(full_report["official_id_file"])
        self.assertFalse((full_output / "test_case_ids_to_generate.json").exists())
        self.assertEqual(
            json.loads((full_output / "bfcl_progress.json").read_text())["group_count"],
            22,
        )
        self.assertEqual(calls, [])

    def test_resume_check_repairs_error_record_and_continues_same_run(self):
        trial_manifest = BUILTIN_BENCHMARKS["trial-3"]
        output = self.root / "interrupted-trial"
        original = run_bfcl(
            model=MODEL,
            bfcl_bin="bfcl",
            manifest_path=trial_manifest,
            output_dir=output,
            dry_run=True,
        )
        original["status"] = "interrupted"
        original["error"] = {"type": "KeyboardInterrupt", "message": "模拟中断"}
        (output / "bfcl_run_report.json").write_text(
            json.dumps(original), encoding="utf-8"
        )
        result_dir = output / "result" / MODEL
        result_dir.mkdir(parents=True)
        (result_dir / "BFCL_v4_simple_python_result.json").write_text(
            json.dumps({
                "id": "simple_python_0",
                "result": [],
                "input_token_count": 7,
                "output_token_count": 2,
                "latency": 0.1,
            }) + "\n",
            encoding="utf-8",
        )
        error_path = result_dir / "BFCL_v4_multiple_result.json"
        error_path.write_text(
            json.dumps({
                "id": "multiple_0",
                "result": "Error during inference: simulated",
            }) + "\n",
            encoding="utf-8",
        )
        stale_score = output / "score" / MODEL / "BFCL_v4_simple_python_score.json"
        stale_score.parent.mkdir(parents=True)
        stale_score.write_text(
            json.dumps({"accuracy": 0.0, "correct_count": 0, "total_count": 1})
            + "\n",
            encoding="utf-8",
        )

        checked = resume_bfcl(run=output, check_only=True)
        self.assertEqual(checked["status"], "ready")
        self.assertEqual(checked["next_attempt"], 2)
        self.assertEqual(checked["audit"]["existing_result_records"], 2)
        self.assertEqual(checked["audit"]["reusable_records"], 1)
        self.assertEqual(checked["audit"]["records_to_retry"], 1)
        self.assertEqual(checked["audit"]["remaining_generation_entries"], 2)
        self.assertEqual(checked["audit"]["stale_score_files_to_remove"], 1)
        self.assertEqual(json.loads(error_path.read_text())["id"], "multiple_0")

        fake = ResumeFakeBFCL()
        report = resume_bfcl(
            run=output,
            env={},
            command_runner=fake,
        )

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["resume_count"], 1)
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(report["attempts"][-1]["number"], 2)
        self.assertEqual(report["attempts"][-1]["status"], "completed")
        self.assertEqual(report["official_outputs"]["scored_cases"], 3)
        self.assertNotIn("--allow-overwrite", fake.calls[-2])
        self.assertTrue(
            (output / "logs" / "attempt_002" / "generate.stdout.log").is_file()
        )
        backup = (
            output / "resume_backups" / "attempt_002" / "result" / MODEL
            / "BFCL_v4_multiple_result.json"
        )
        self.assertTrue(backup.is_file())
        self.assertIn("Error during inference", backup.read_text(encoding="utf-8"))
        self.assertTrue(
            (output / "resume_backups" / "attempt_002" / "score" / MODEL
             / "BFCL_v4_simple_python_score.json").is_file()
        )
        self.assertEqual(
            json.loads((output / "bfcl_progress.json").read_text())["generation"]
            ["completed_entries"],
            3,
        )

    def test_memory_resume_rolls_back_to_contiguous_checkpoint_prefix(self):
        manifest = load_manifest(BUILTIN_BENCHMARKS["all-scoring"])
        result_dir = self.root / "result" / MODEL / "memory_rec_sum"
        checkpoint_dir = result_dir / "memory_snapshot" / "prereq_checkpoints"
        checkpoint_dir.mkdir(parents=True)
        result_path = result_dir / "BFCL_v4_memory_rec_sum_prereq_result.json"
        ids = [
            "memory_rec_sum_prereq_0-healthcare-0",
            "memory_rec_sum_prereq_1-healthcare-1",
            "memory_rec_sum_prereq_2-healthcare-2",
        ]
        result_path.write_text("".join(
            json.dumps({
                "id": case_id,
                "result": (
                    "Error during inference: simulated" if index == 1 else []
                ),
            }) + "\n"
            for index, case_id in enumerate(ids)
        ), encoding="utf-8")
        (checkpoint_dir / f"{ids[0]}.json").write_text(
            json.dumps({"state": "step-0"}), encoding="utf-8"
        )
        orphan = checkpoint_dir / f"{ids[2]}.json"
        orphan.write_text(json.dumps({"state": "step-2"}), encoding="utf-8")
        final_snapshot = result_dir / "memory_snapshot" / "healthcare_final.json"
        final_snapshot.write_text(json.dumps({"state": "step-2"}), encoding="utf-8")

        audit = audit_resume_state(self.root, manifest)
        self.assertEqual(audit["existing_result_records"], 3)
        self.assertEqual(audit["reusable_records"], 1)
        self.assertEqual(audit["records_to_retry"], 2)
        self.assertEqual(audit["valid_memory_checkpoints"], 1)
        self.assertEqual(audit["memory_scenarios_to_repair"], 1)

        repaired = repair_resume_state(self.root, manifest, attempt_number=2)
        self.assertEqual(repaired["post_repair_reusable_records"], 1)
        remaining = [json.loads(line) for line in result_path.read_text().splitlines()]
        self.assertEqual([item["id"] for item in remaining], [ids[0]])
        self.assertFalse(orphan.exists())
        self.assertEqual(
            json.loads(final_snapshot.read_text(encoding="utf-8")),
            {"state": "step-0"},
        )
        self.assertTrue(
            (self.root / "resume_backups" / "attempt_002" /
             result_path.relative_to(self.root)).is_file()
        )
        post = audit_resume_state(self.root, manifest)
        self.assertEqual(post["records_to_retry"], 0)
        self.assertEqual(post["memory_scenarios_to_repair"], 0)

    def test_resume_audit_rejects_duplicate_result_ids(self):
        result_dir = self.root / "result" / MODEL
        result_dir.mkdir(parents=True)
        path = result_dir / "BFCL_v4_simple_python_result.json"
        row = json.dumps({"id": "simple_python_0", "result": []}) + "\n"
        path.write_text(row + row, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "重复 id"):
            audit_resume_state(self.root, load_manifest())

    def test_collect_official_outputs_rejects_a_score_without_header(self):
        path = self.root / "score" / MODEL / "BFCL_v4_simple_python_score.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "simple_python_0", "valid": False}) + "\n",
                        encoding="utf-8")

        with self.assertRaises(ValueError):
            collect_official_outputs(self.root)


if __name__ == "__main__":
    unittest.main()
