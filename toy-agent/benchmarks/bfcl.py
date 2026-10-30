#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BFCL 官方 harness 的旁路运行器。

本模块不复制 BFCL 数据、prompt 或 scorer。它只负责：

1. 校验并写出官方 ``test_case_ids_to_generate.json``；
2. 在独立 ``BFCL_PROJECT_ROOT`` 下调用官方 ``bfcl generate/evaluate``；
3. 保留 stdout/stderr 和官方 result/score；
4. 把官方 score JSON 的首行汇总到一份可追溯报告中。

真实模型调用和评分始终由固定版本的 ``bfcl-eval`` 完成。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable

from agent import find_env_files, load_env


PINNED_BFCL_VERSION = "2026.3.23"
PACKAGE_NAME = "bfcl-eval"
BENCHMARK_NAME = "BFCL"
BENCHMARK_VERSION = "V4"
DEFAULT_MODEL = "DeepSeek-V3.2-Exp-FC"
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifests" / "bfcl_v4_single_turn_smoke.json"
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "benchmark_runs" / "bfcl"

CommandRunner = Callable[[list[str], dict[str, str], Path], dict]


@dataclass(frozen=True)
class BFCLManifest:
    """一组固定的官方 BFCL case id。"""

    path: Path
    name: str
    profile: str
    benchmark_version: str
    harness_version: str
    categories: dict[str, list[str]]

    @property
    def case_ids(self) -> list[str]:
        return [case_id for ids in self.categories.values() for case_id in ids]


def load_bfcl_env(start: Path | str | None = None) -> list[Path]:
    """加载项目附近的 ``.env``，且不覆盖 shell 中已有的环境变量。"""
    search_start = (
        Path(start).expanduser().resolve()
        if start is not None
        else Path(__file__).resolve().parent.parent
    )
    return load_env(find_env_files(search_start))


def _prepare_runtime_env(model: str, env: dict[str, str]) -> tuple[dict[str, str], bool]:
    """为官方 DeepSeek handler 的父类初始化补充进程内兼容别名。"""
    runtime_env = dict(env)
    bridged = False
    if (
        model.startswith("DeepSeek-")
        and runtime_env.get("DEEPSEEK_API_KEY")
        and not runtime_env.get("OPENAI_API_KEY")
    ):
        # bfcl-eval 2026.3.23 的 DeepSeek handler 会先初始化 OpenAI 父类，
        # 随后才用 DEEPSEEK_API_KEY 重建真正访问 DeepSeek endpoint 的 client。
        # 新版 OpenAI SDK 要求父类初始化时也必须有 key，因此只在子进程环境中
        # 提供同值别名；不写回 os.environ、.env 或运行报告。
        runtime_env["OPENAI_API_KEY"] = runtime_env["DEEPSEEK_API_KEY"]
        bridged = True
    return runtime_env, bridged


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def load_manifest(path: Path | str = DEFAULT_MANIFEST) -> BFCLManifest:
    """读取并严格校验 BFCL 子集 manifest。"""
    target = Path(path).expanduser().resolve()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"BFCL manifest 不是有效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("BFCL manifest 必须是 JSON 对象")
    if data.get("benchmark") != BENCHMARK_NAME:
        raise ValueError(f"benchmark 必须是 {BENCHMARK_NAME!r}")

    name = _require_text(data.get("name"), "name")
    profile = _require_text(data.get("profile"), "profile")
    benchmark_version = _require_text(data.get("benchmark_version"), "benchmark_version")
    harness = data.get("harness")
    if not isinstance(harness, dict) or harness.get("package") != PACKAGE_NAME:
        raise ValueError(f"harness.package 必须是 {PACKAGE_NAME!r}")
    harness_version = _require_text(harness.get("version"), "harness.version")
    if harness_version != PINNED_BFCL_VERSION:
        raise ValueError(
            f"manifest 固定的 bfcl-eval 版本是 {harness_version}，"
            f"当前适配器要求 {PINNED_BFCL_VERSION}"
        )

    raw_categories = data.get("categories")
    if not isinstance(raw_categories, dict) or not raw_categories:
        raise ValueError("categories 必须是非空对象")
    categories: dict[str, list[str]] = {}
    all_ids: list[str] = []
    for raw_category, raw_ids in raw_categories.items():
        category = _require_text(raw_category, "category")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError(f"类别 {category!r} 必须包含至少一个 case id")
        ids = []
        for raw_id in raw_ids:
            case_id = _require_text(raw_id, f"{category} case id")
            if not case_id.startswith(f"{category}_"):
                raise ValueError(f"case id {case_id!r} 与类别 {category!r} 不匹配")
            ids.append(case_id)
        categories[category] = ids
        all_ids.extend(ids)
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("BFCL manifest 中的 case id 不能重复")

    return BFCLManifest(
        path=target,
        name=name,
        profile=profile,
        benchmark_version=benchmark_version,
        harness_version=harness_version,
        categories=categories,
    )


def official_id_payload(manifest: BFCLManifest) -> dict[str, list[str]]:
    """转换成 BFCL 官方 ``--run-ids`` 所需的纯 category -> ids 格式。"""
    return {category: list(ids) for category, ids in manifest.categories.items()}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _atomic_write_text(path, payload)


def write_official_id_file(manifest: BFCLManifest, project_root: Path | str) -> Path:
    target = Path(project_root).expanduser().resolve() / "test_case_ids_to_generate.json"
    _atomic_write_json(target, official_id_payload(manifest))
    return target


def build_commands(
    bfcl_bin: str,
    model: str,
    manifest: BFCLManifest,
    *,
    temperature: float = 0.001,
    num_threads: int = 1,
    allow_overwrite: bool = False,
) -> tuple[list[str], list[str]]:
    """构造官方 CLI 命令；不通过 shell 拼接。"""
    _require_text(bfcl_bin, "bfcl_bin")
    _require_text(model, "model")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise ValueError("temperature 必须是数字")
    if type(num_threads) is not int or num_threads < 1:
        raise ValueError("num_threads 必须是正整数")

    generate = [
        bfcl_bin,
        "generate",
        "--model",
        model,
        "--run-ids",
        "--temperature",
        str(temperature),
        "--num-threads",
        str(num_threads),
    ]
    if allow_overwrite:
        generate.append("--allow-overwrite")
    evaluate = [
        bfcl_bin,
        "evaluate",
        "--model",
        model,
        "--test-category",
        ",".join(manifest.categories),
        "--partial-eval",
    ]
    return generate, evaluate


def _default_command_runner(command: list[str], env: dict[str, str], cwd: Path) -> dict:
    started = perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "duration_seconds": perf_counter() - started,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _invoke(command_runner: CommandRunner, command: list[str],
            env: dict[str, str], cwd: Path) -> dict:
    """把启动失败也转换成 doctor 可报告的命令结果。"""
    try:
        return command_runner(command, env, cwd)
    except OSError as exc:
        return {
            "command": list(command),
            "returncode": None,
            "duration_seconds": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def _parse_version(output: str) -> str | None:
    # 只接受数字开头的版本号，避免把官方 traceback 中的源码片段
    # ``bfcl version: {_version('bfcl')}`` 误识别为实际版本。
    match = re.search(
        r"bfcl(?:-eval)?\s+version:\s*([0-9]+(?:\.[0-9]+)+(?:[-+._A-Za-z0-9]*)?)",
        output,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _bfcl_python(bfcl_bin: str) -> str | None:
    resolved = shutil.which(bfcl_bin) if not Path(bfcl_bin).is_file() else str(Path(bfcl_bin).resolve())
    if not resolved:
        return None
    launcher = Path(resolved)
    try:
        first_line = launcher.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        first_line = ""
    # pip 在路径含空格时会生成 ``#!/bin/sh`` 的 polyglot launcher。此时
    # ``/bin/sh -c ...`` 不是我们要找的 Python；优先回到同一 venv 的
    # sibling interpreter。普通 ``#!/.../python`` launcher 仍可直接使用。
    if first_line.startswith("#!") and "python" in Path(first_line[2:].strip()).name.lower():
        interpreter = first_line[2:].strip()
        if Path(interpreter).is_file():
            return interpreter
    for name in ("python", "python3"):
        candidate = launcher.parent / name
        if candidate.is_file():
            return str(candidate)
    return None


def probe_bfcl(
    bfcl_bin: str,
    model: str,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    credential_env: str | None = None,
    command_runner: CommandRunner = _default_command_runner,
) -> dict:
    """检查官方 CLI 版本、模型注册和可选凭据，不发起模型请求。"""
    runtime_env = dict(os.environ if env is None else env)
    working = Path(cwd or Path.cwd()).resolve()
    checks = []

    version_run = _invoke(command_runner, [bfcl_bin, "version"], runtime_env, working)
    version_output = (version_run.get("stdout") or "") + "\n" + (version_run.get("stderr") or "")
    detected = _parse_version(version_output)
    # 2026.3.23 的官方 console command 在部分安装中把 distribution 名写成
    # ``bfcl`` 而不是 ``bfcl-eval``。若 version 子命令失败，使用同一 venv 的
    # Python 查询官方 distribution 元数据，不影响 generate/evaluate。
    fallback_run = None
    if detected is None:
        python = _bfcl_python(bfcl_bin)
        if python is not None:
            fallback_run = _invoke(
                command_runner,
                [
                    python,
                    "-c",
                    "import importlib.metadata as m; print('bfcl-eval version:', m.version('bfcl-eval'))",
                ],
                runtime_env,
                working,
            )
            fallback_output = (fallback_run.get("stdout") or "") + "\n" + (fallback_run.get("stderr") or "")
            detected = _parse_version(fallback_output)
    checks.append({
        "name": "version",
        "ok": detected == PINNED_BFCL_VERSION,
        "expected": PINNED_BFCL_VERSION,
        "actual": detected,
        "command_returncode": version_run.get("returncode"),
        "fallback_returncode": None if fallback_run is None else fallback_run.get("returncode"),
    })

    models_run = _invoke(command_runner, [bfcl_bin, "models"], runtime_env, working)
    models = {line.strip() for line in (models_run.get("stdout") or "").splitlines() if line.strip()}
    checks.append({
        "name": "model",
        "ok": models_run.get("returncode") == 0 and model in models,
        "expected": model,
        "actual": model if model in models else None,
        "command_returncode": models_run.get("returncode"),
    })

    if credential_env:
        credential = runtime_env.get(credential_env)
        checks.append({
            "name": "credential",
            "ok": isinstance(credential, str) and bool(credential.strip()),
            "expected": f"环境变量 {credential_env} 已设置",
            "actual": "present" if credential else "missing",
        })

    return {
        "ok": all(check["ok"] for check in checks),
        "bfcl_bin": bfcl_bin,
        "model": model,
        "checks": checks,
    }


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")[:64] or "model"


def _new_run_root(runs_dir: Path, model: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return runs_dir / f"{timestamp}_{_safe_component(model)}_{uuid.uuid4().hex[:8]}"


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_lines(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} 第 {line_number} 行不是有效 JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{path} 第 {line_number} 行必须是 JSON 对象")
        records.append(item)
    return records


def collect_official_outputs(project_root: Path | str) -> dict:
    """读取官方文件，不重新判断单条样例对错。"""
    root = Path(project_root).expanduser().resolve()
    category_scores = []
    score_files = sorted((root / "score").rglob("BFCL_v*_score.json")) if (root / "score").exists() else []
    for path in score_files:
        records = _json_lines(path)
        if not records:
            raise ValueError(f"官方 score 文件为空: {path}")
        header = records[0]
        fields = ("accuracy", "correct_count", "total_count")
        if any(field not in header for field in fields):
            raise ValueError(f"官方 score 文件缺少汇总头: {path}")
        match = re.match(r"BFCL_v\d+_(.+)_score\.json$", path.name)
        category_scores.append({
            "category": match.group(1) if match else path.stem,
            "accuracy": header["accuracy"],
            "correct_count": header["correct_count"],
            "total_count": header["total_count"],
            "incorrect_records": len(records) - 1,
            "path": _relative(path, root),
            "sha256": _sha256(path),
        })

    result_files = []
    input_tokens = 0
    output_tokens = 0
    model_latency_seconds = 0.0
    result_root = root / "result"
    if result_root.exists():
        for path in sorted(result_root.rglob("BFCL_v*_result.json")):
            records = _json_lines(path)
            file_input_tokens = sum(
                item["input_token_count"] for item in records
                if type(item.get("input_token_count")) is int
            )
            file_output_tokens = sum(
                item["output_token_count"] for item in records
                if type(item.get("output_token_count")) is int
            )
            file_latency = sum(
                float(item["latency"]) for item in records
                if isinstance(item.get("latency"), (int, float))
                and not isinstance(item.get("latency"), bool)
            )
            input_tokens += file_input_tokens
            output_tokens += file_output_tokens
            model_latency_seconds += file_latency
            result_files.append({
                "path": _relative(path, root),
                "record_count": len(records),
                "input_tokens": file_input_tokens,
                "output_tokens": file_output_tokens,
                "model_latency_seconds": file_latency,
                "sha256": _sha256(path),
            })

    correct = sum(item["correct_count"] for item in category_scores)
    total = sum(item["total_count"] for item in category_scores)
    csv_files = [
        {"path": _relative(path, root), "sha256": _sha256(path)}
        for path in sorted((root / "score").glob("*.csv"))
    ] if (root / "score").exists() else []
    return {
        "official_category_scores": category_scores,
        "derived_subset_micro_accuracy": correct / total if total else None,
        "correct_cases": correct,
        "scored_cases": total,
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "model_latency_seconds": model_latency_seconds,
        "result_files": result_files,
        "official_csv_files": csv_files,
    }


def _command_record(result: dict, log_dir: Path, name: str, root: Path) -> dict:
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    _atomic_write_text(stdout_path, result.get("stdout") or "")
    _atomic_write_text(stderr_path, result.get("stderr") or "")
    return {
        "command": list(result.get("command") or []),
        "command_display": shlex.join(result.get("command") or []),
        "returncode": result.get("returncode"),
        "duration_seconds": result.get("duration_seconds"),
        "stdout_log": _relative(stdout_path, root),
        "stderr_log": _relative(stderr_path, root),
    }


def _git_commit(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def run_bfcl(
    *,
    model: str,
    bfcl_bin: str,
    manifest_path: Path | str = DEFAULT_MANIFEST,
    runs_dir: Path | str = DEFAULT_RUNS_DIR,
    output_dir: Path | str | None = None,
    temperature: float = 0.001,
    num_threads: int = 1,
    allow_overwrite: bool = False,
    credential_env: str | None = None,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    command_runner: CommandRunner = _default_command_runner,
) -> dict:
    """运行固定 BFCL 子集，并始终尝试保存编排报告。"""
    manifest = load_manifest(manifest_path)
    root = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else _new_run_root(Path(runs_dir).expanduser().resolve(), model)
    )
    root.mkdir(parents=True, exist_ok=False)
    log_dir = root / "logs"
    id_file = write_official_id_file(manifest, root)
    generate_command, evaluate_command = build_commands(
        bfcl_bin, model, manifest, temperature=temperature,
        num_threads=num_threads, allow_overwrite=allow_overwrite,
    )
    runtime_env, deepseek_bootstrap_alias = _prepare_runtime_env(
        model, dict(os.environ if env is None else env)
    )
    runtime_env["BFCL_PROJECT_ROOT"] = str(root)
    repo_root = Path(__file__).resolve().parents[2]

    report = {
        "kind": "bfcl_official_run",
        "status": "preparing",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": {
            "name": BENCHMARK_NAME,
            "version": manifest.benchmark_version,
            "profile": manifest.profile,
            "subset": True,
            "manifest_name": manifest.name,
            "manifest_path": str(manifest.path),
            "case_count": len(manifest.case_ids),
            "categories": {key: list(value) for key, value in manifest.categories.items()},
        },
        "official_harness": {
            "package": PACKAGE_NAME,
            "required_version": manifest.harness_version,
            "bfcl_bin": bfcl_bin,
        },
        "model": model,
        "config": {
            "temperature": temperature,
            "num_threads": num_threads,
            "allow_overwrite": allow_overwrite,
            "credential_env": credential_env,
            "deepseek_openai_bootstrap_alias": deepseek_bootstrap_alias,
        },
        "toy_agent_commit": _git_commit(repo_root),
        "project_root": str(root),
        "official_id_file": _relative(id_file, root),
        "commands": {
            "generate": {"command": generate_command, "command_display": shlex.join(generate_command)},
            "evaluate": {"command": evaluate_command, "command_display": shlex.join(evaluate_command)},
        },
        "doctor": None,
        "official_outputs": None,
        "error": None,
        "report_path": str(root / "bfcl_run_report.json"),
    }
    report_path = root / "bfcl_run_report.json"

    if dry_run:
        report["status"] = "dry_run"
        _atomic_write_json(report_path, report)
        return report

    try:
        doctor = probe_bfcl(
            bfcl_bin, model, env=runtime_env, cwd=root,
            credential_env=credential_env, command_runner=command_runner,
        )
        report["doctor"] = doctor
        if not doctor["ok"]:
            report["status"] = "setup_error"
            report["error"] = {"type": "BFCLDoctorError", "message": "BFCL 环境检查未通过"}
            return report

        generation = command_runner(generate_command, runtime_env, root)
        report["commands"]["generate"] = _command_record(generation, log_dir, "generate", root)
        if generation.get("returncode") != 0:
            report["status"] = "generation_error"
            report["error"] = {
                "type": "BFCLGenerationError",
                "message": "官方 bfcl generate 返回非零状态",
            }
            report["official_outputs"] = collect_official_outputs(root)
            return report

        evaluation = command_runner(evaluate_command, runtime_env, root)
        report["commands"]["evaluate"] = _command_record(evaluation, log_dir, "evaluate", root)
        report["official_outputs"] = collect_official_outputs(root)
        if evaluation.get("returncode") != 0:
            report["status"] = "evaluation_error"
            report["error"] = {
                "type": "BFCLEvaluationError",
                "message": "官方 bfcl evaluate 返回非零状态",
            }
            return report
        if not report["official_outputs"]["official_category_scores"]:
            report["status"] = "evaluation_error"
            report["error"] = {
                "type": "BFCLMissingScoreError",
                "message": "官方评测命令成功，但没有找到 category score 文件",
            }
            return report
        report["status"] = "completed"
        return report
    except (OSError, ValueError, TypeError) as exc:
        report["status"] = "orchestration_error"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        try:
            report["official_outputs"] = collect_official_outputs(root)
        except (OSError, ValueError, TypeError):
            pass
        return report
    finally:
        _atomic_write_json(report_path, report)


def _default_bfcl_bin() -> str:
    local = Path(__file__).resolve().parent.parent / ".venv-bfcl" / "bin" / "bfcl"
    return str(local) if local.is_file() else "bfcl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BFCL 官方 harness 旁路集成")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查版本、模型注册和凭据，不调用模型")
    doctor_parser.add_argument("--bfcl-bin", default=_default_bfcl_bin())
    doctor_parser.add_argument("--model", default=DEFAULT_MODEL)
    doctor_parser.add_argument("--credential-env", default="DEEPSEEK_API_KEY")

    run_parser = subparsers.add_parser("run", help="运行固定子集并调用官方 scorer")
    run_parser.add_argument("--bfcl-bin", default=_default_bfcl_bin())
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    run_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_parser.add_argument("--output-dir", type=Path, default=None)
    run_parser.add_argument("--temperature", type=float, default=0.001)
    run_parser.add_argument("--num-threads", type=int, default=1)
    run_parser.add_argument("--allow-overwrite", action="store_true")
    run_parser.add_argument("--credential-env", default="DEEPSEEK_API_KEY")
    run_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    # doctor 和真实 run 需要凭据时才读取 .env；dry-run 不接触密钥文件。
    if args.command == "doctor" or not args.dry_run:
        load_bfcl_env()
    if args.command == "doctor":
        result = probe_bfcl(
            args.bfcl_bin, args.model, credential_env=args.credential_env or None
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result["ok"] else 1

    result = run_bfcl(
        model=args.model,
        bfcl_bin=args.bfcl_bin,
        manifest_path=args.manifest,
        runs_dir=args.runs_dir,
        output_dir=args.output_dir,
        temperature=args.temperature,
        num_threads=args.num_threads,
        allow_overwrite=args.allow_overwrite,
        credential_env=args.credential_env or None,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in {"completed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
