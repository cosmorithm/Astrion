"""BFCL 官方旁路适配层的离线测试；不安装 BFCL、不访问网络。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.bfcl import (
    DEFAULT_MANIFEST,
    PINNED_BFCL_VERSION,
    _bfcl_python,
    _parse_version,
    build_commands,
    collect_official_outputs,
    load_bfcl_env,
    load_manifest,
    official_id_payload,
    probe_bfcl,
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


class BFCLTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(prefix="toy-agent-bfcl-")
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)

    def test_default_manifest_is_a_fixed_two_hundred_case_subset(self):
        manifest = load_manifest()

        self.assertEqual(manifest.path, DEFAULT_MANIFEST.resolve())
        self.assertEqual(manifest.harness_version, PINNED_BFCL_VERSION)
        self.assertEqual(manifest.benchmark_version, "V4")
        self.assertEqual(manifest.profile, "subset")
        self.assertEqual(len(manifest.categories), 5)
        self.assertTrue(all(len(ids) == 40 for ids in manifest.categories.values()))
        self.assertEqual(len(manifest.case_ids), 200)
        self.assertEqual(len(set(manifest.case_ids)), 200)

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

    def test_official_id_file_contains_only_the_bfcl_mapping(self):
        manifest = load_manifest()
        path = write_official_id_file(manifest, self.root)

        self.assertEqual(path, (self.root / "test_case_ids_to_generate.json").resolve())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         official_id_payload(manifest))
        self.assertNotIn("harness", json.loads(path.read_text(encoding="utf-8")))

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

    def test_collect_official_outputs_rejects_a_score_without_header(self):
        path = self.root / "score" / MODEL / "BFCL_v4_simple_python_score.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "simple_python_0", "valid": False}) + "\n",
                        encoding="utf-8")

        with self.assertRaises(ValueError):
            collect_official_outputs(self.root)


if __name__ == "__main__":
    unittest.main()
