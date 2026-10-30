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
from time import monotonic, perf_counter, sleep
from typing import Callable

from agent import find_env_files, load_env


PINNED_BFCL_VERSION = "2026.3.23"
PACKAGE_NAME = "bfcl-eval"
BENCHMARK_NAME = "BFCL"
BENCHMARK_VERSION = "V4"
DEFAULT_MODEL = "DeepSeek-V3.2-Exp-FC"
MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"
BUILTIN_BENCHMARKS = {
    "baseline-200": MANIFESTS_DIR / "bfcl_v4_single_turn_baseline_200.json",
    "smoke-20": MANIFESTS_DIR / "bfcl_v4_single_turn_smoke.json",
    "trial-3": MANIFESTS_DIR / "bfcl_v4_single_turn_trial_3.json",
    "all-scoring": MANIFESTS_DIR / "bfcl_v4_all_scoring_full.json",
}
DEFAULT_BENCHMARK = "baseline-200"
DEFAULT_MANIFEST = BUILTIN_BENCHMARKS[DEFAULT_BENCHMARK]
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "benchmark_runs" / "bfcl"

CommandRunner = Callable[[list[str], dict[str, str], Path], dict]


@dataclass(frozen=True)
class BFCLManifest:
    """固定 case id 子集或官方 collection 基准。"""

    path: Path
    name: str
    profile: str
    benchmark_version: str
    harness_version: str
    categories: dict[str, list[str]]
    category_counts: dict[str, int]
    selection_mode: str
    official_collection: str | None
    expected_generation_entries: int
    expected_scored_cases: int

    @property
    def case_ids(self) -> list[str]:
        return [case_id for ids in self.categories.values() for case_id in ids]

    @property
    def uses_run_ids(self) -> bool:
        return self.selection_mode == "case_ids"

    @property
    def case_count(self) -> int:
        return self.expected_generation_entries


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

    selection = data.get("selection")
    selection_mode = "case_ids"
    official_collection = None
    expected_scored_cases = 0
    if selection is not None:
        if not isinstance(selection, dict):
            raise ValueError("selection 必须是 JSON 对象")
        selection_mode = _require_text(selection.get("mode"), "selection.mode")

    if selection_mode == "official_collection":
        official_collection = _require_text(
            selection.get("collection"), "selection.collection"
        )
        if official_collection != "all_scoring":
            raise ValueError("当前只支持官方 collection 'all_scoring'")
        expected_generation_entries = selection.get("expected_generation_entries")
        expected_scored_cases = selection.get("expected_scored_cases")
        if type(expected_generation_entries) is not int or expected_generation_entries < 1:
            raise ValueError("selection.expected_generation_entries 必须是正整数")
        if (
            type(expected_scored_cases) is not int
            or expected_scored_cases < 1
            or expected_scored_cases > expected_generation_entries
        ):
            raise ValueError("selection.expected_scored_cases 必须是有效正整数")
        raw_counts = data.get("category_counts")
        if not isinstance(raw_counts, dict) or not raw_counts:
            raise ValueError("official_collection manifest 需要 category_counts")
        category_counts = {}
        for raw_category, raw_count in raw_counts.items():
            category = _require_text(raw_category, "category_counts category")
            if type(raw_count) is not int or raw_count < 1:
                raise ValueError(f"类别 {category!r} 的数量必须是正整数")
            category_counts[category] = raw_count
        if sum(category_counts.values()) != expected_generation_entries:
            raise ValueError("category_counts 总和与 expected_generation_entries 不一致")
        return BFCLManifest(
            path=target,
            name=name,
            profile=profile,
            benchmark_version=benchmark_version,
            harness_version=harness_version,
            categories={},
            category_counts=category_counts,
            selection_mode=selection_mode,
            official_collection=official_collection,
            expected_generation_entries=expected_generation_entries,
            expected_scored_cases=expected_scored_cases,
        )
    if selection_mode != "case_ids":
        raise ValueError(f"不支持的 selection.mode: {selection_mode!r}")

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

    category_counts = {category: len(ids) for category, ids in categories.items()}
    expected_generation_entries = len(all_ids)

    return BFCLManifest(
        path=target,
        name=name,
        profile=profile,
        benchmark_version=benchmark_version,
        harness_version=harness_version,
        categories=categories,
        category_counts=category_counts,
        selection_mode=selection_mode,
        official_collection=None,
        expected_generation_entries=expected_generation_entries,
        expected_scored_cases=expected_generation_entries,
    )


def official_id_payload(manifest: BFCLManifest) -> dict[str, list[str]]:
    """转换成 BFCL 官方 ``--run-ids`` 所需的纯 category -> ids 格式。"""
    if not manifest.uses_run_ids:
        raise ValueError("official_collection 基准不使用 test_case_ids_to_generate.json")
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
    ]
    if manifest.uses_run_ids:
        generate.append("--run-ids")
        evaluate_category = ",".join(manifest.categories)
    else:
        generate.extend(["--test-category", manifest.official_collection])
        evaluate_category = manifest.official_collection
    generate.extend([
        "--temperature",
        str(temperature),
        "--num-threads",
        str(num_threads),
    ])
    if allow_overwrite:
        generate.append("--allow-overwrite")
    evaluate = [
        bfcl_bin,
        "evaluate",
        "--model",
        model,
        "--test-category",
        evaluate_category,
    ]
    if manifest.uses_run_ids:
        evaluate.append("--partial-eval")
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


def _count_nonempty_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            count = 0
            for line in handle:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    # 写入线程可能正处于一行中间；下次轮询再计入。
                    continue
                count += 1
            return count
    except OSError:
        return 0


def _artifact_category(path: Path, artifact: str) -> str | None:
    match = re.match(rf"BFCL_v\d+_(.+)_{artifact}\.json$", path.name)
    if not match:
        return None
    category = match.group(1)
    if artifact == "result" and category.endswith("_prereq"):
        category = category.removesuffix("_prereq")
    return category


def build_progress_snapshot(
    project_root: Path | str,
    manifest: BFCLManifest,
    phase: str,
) -> dict:
    """从正在写入的官方文件计算类别组和总进度。"""
    root = Path(project_root).expanduser().resolve()
    category_names = list(manifest.category_counts)
    generated_by_category = {category: 0 for category in category_names}
    result_mtimes: dict[str, int] = {}
    result_root = root / "result"
    if result_root.exists():
        for path in result_root.rglob("BFCL_v*_result.json"):
            category = _artifact_category(path, "result")
            if category not in generated_by_category:
                continue
            generated_by_category[category] += _count_nonempty_lines(path)
            try:
                result_mtimes[category] = max(
                    result_mtimes.get(category, 0), path.stat().st_mtime_ns
                )
            except OSError:
                pass

    scored_categories = set()
    score_mtimes: dict[str, int] = {}
    score_root = root / "score"
    if score_root.exists():
        for path in score_root.rglob("BFCL_v*_score.json"):
            category = _artifact_category(path, "score")
            if category not in manifest.category_counts:
                continue
            scored_categories.add(category)
            try:
                score_mtimes[category] = max(
                    score_mtimes.get(category, 0), path.stat().st_mtime_ns
                )
            except OSError:
                pass

    completed_generation = [
        category for category in category_names
        if generated_by_category[category] >= manifest.category_counts[category]
    ]
    generated_entries = sum(generated_by_category.values())
    generation_percent = (
        generated_entries / manifest.expected_generation_entries
        if manifest.expected_generation_entries
        else 0.0
    )

    if phase == "evaluation" and score_mtimes:
        active_category = max(score_mtimes, key=score_mtimes.get)
    elif result_mtimes:
        active_category = max(result_mtimes, key=result_mtimes.get)
    else:
        active_category = None

    current_group = None
    if active_category is not None:
        current_group = {
            "position": category_names.index(active_category) + 1,
            "total_groups": len(category_names),
            "category": active_category,
            "generated_entries": generated_by_category[active_category],
            "expected_entries": manifest.category_counts[active_category],
            "generation_complete": active_category in completed_generation,
            "score_file_present": active_category in scored_categories,
        }

    return {
        "kind": "bfcl_progress",
        "phase": phase,
        "run_id": root.name,
        "manifest_name": manifest.name,
        "profile": manifest.profile,
        "official_collection": manifest.official_collection,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "group_count": len(category_names),
        "completed_generation_groups": len(completed_generation),
        "scored_groups": len(scored_categories),
        "current_group": current_group,
        "generation": {
            "completed_entries": generated_entries,
            "expected_entries": manifest.expected_generation_entries,
            "percent": generation_percent,
            "by_category": generated_by_category,
        },
        "evaluation": {
            "completed_groups": len(scored_categories),
            "expected_groups": len(category_names),
        },
        "progress_path": str(root / "bfcl_progress.json"),
    }


def _write_progress(project_root: Path, manifest: BFCLManifest, phase: str) -> dict:
    snapshot = build_progress_snapshot(project_root, manifest, phase)
    _atomic_write_json(project_root / "bfcl_progress.json", snapshot)
    return snapshot


def format_progress(snapshot: dict) -> str:
    """生成一行适合终端观察的进度。"""
    phase = snapshot["phase"]
    current = snapshot.get("current_group")
    group_text = "尚未写出首条结果"
    if current is not None:
        group_text = (
            f"当前/最近组 {current['position']}/{current['total_groups']} "
            f"{current['category']} "
            f"{current['generated_entries']}/{current['expected_entries']}"
        )
    generation = snapshot["generation"]
    return (
        f"[BFCL进度] {phase} | {group_text} | "
        f"生成完成组 {snapshot['completed_generation_groups']}/{snapshot['group_count']} | "
        f"总条目 {generation['completed_entries']}/{generation['expected_entries']} "
        f"({generation['percent']:.2%}) | "
        f"评分完成组 {snapshot['scored_groups']}/{snapshot['group_count']}"
    )


def _progress_command_runner(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
    *,
    manifest: BFCLManifest,
    phase: str,
    interval_seconds: float,
) -> dict:
    """执行长命令，同时轮询官方文件并输出分组进度。"""
    started = perf_counter()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        last_signature = None
        last_printed = 0.0
        progress_step = max(1, manifest.expected_generation_entries // 100)
        try:
            while process.poll() is None:
                snapshot = _write_progress(cwd, manifest, phase)
                current = snapshot.get("current_group") or {}
                signature = (
                    snapshot["phase"],
                    current.get("category"),
                    snapshot["generation"]["completed_entries"] // progress_step,
                    snapshot["completed_generation_groups"],
                    snapshot["scored_groups"],
                )
                now = monotonic()
                if signature != last_signature or now - last_printed >= 60:
                    print(format_progress(snapshot), file=sys.stderr, flush=True)
                    last_signature = signature
                    last_printed = now
                sleep(interval_seconds)
            returncode = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise

        snapshot = _write_progress(cwd, manifest, phase)
        current = snapshot.get("current_group") or {}
        final_signature = (
            snapshot["phase"],
            current.get("category"),
            snapshot["generation"]["completed_entries"] // progress_step,
            snapshot["completed_generation_groups"],
            snapshot["scored_groups"],
        )
        if final_signature != last_signature:
            print(format_progress(snapshot), file=sys.stderr, flush=True)
        stdout_file.seek(0)
        stderr_file.seek(0)
        return {
            "command": list(command),
            "returncode": returncode,
            "duration_seconds": perf_counter() - started,
            "stdout": stdout_file.read(),
            "stderr": stderr_file.read(),
        }


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
    progress_interval: float = 5.0,
    env: dict[str, str] | None = None,
    command_runner: CommandRunner = _default_command_runner,
) -> dict:
    """运行所选 BFCL 子集或官方 collection，并始终尝试保存编排报告。"""
    if (
        not isinstance(progress_interval, (int, float))
        or isinstance(progress_interval, bool)
        or progress_interval <= 0
    ):
        raise ValueError("progress_interval 必须是正数")
    manifest = load_manifest(manifest_path)
    root = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else _new_run_root(Path(runs_dir).expanduser().resolve(), model)
    )
    root.mkdir(parents=True, exist_ok=False)
    log_dir = root / "logs"
    id_file = write_official_id_file(manifest, root) if manifest.uses_run_ids else None
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
            "subset": manifest.uses_run_ids,
            "selection_mode": manifest.selection_mode,
            "official_collection": manifest.official_collection,
            "manifest_name": manifest.name,
            "manifest_path": str(manifest.path),
            "case_count": manifest.case_count,
            "expected_scored_cases": manifest.expected_scored_cases,
            "category_counts": dict(manifest.category_counts),
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
            "progress_interval_seconds": progress_interval,
        },
        "toy_agent_commit": _git_commit(repo_root),
        "project_root": str(root),
        "official_id_file": _relative(id_file, root) if id_file is not None else None,
        "commands": {
            "generate": {"command": generate_command, "command_display": shlex.join(generate_command)},
            "evaluate": {"command": evaluate_command, "command_display": shlex.join(evaluate_command)},
        },
        "doctor": None,
        "official_outputs": None,
        "error": None,
        "report_path": str(root / "bfcl_run_report.json"),
        "progress_path": str(root / "bfcl_progress.json"),
    }
    report_path = root / "bfcl_run_report.json"
    _atomic_write_json(report_path, report)
    _write_progress(root, manifest, "preparing")

    if dry_run:
        report["status"] = "dry_run"
        _atomic_write_json(report_path, report)
        _write_progress(root, manifest, "dry_run")
        return report

    def run_long_command(command: list[str], phase: str) -> dict:
        _write_progress(root, manifest, phase)
        if command_runner is _default_command_runner:
            return _progress_command_runner(
                command,
                runtime_env,
                root,
                manifest=manifest,
                phase=phase,
                interval_seconds=float(progress_interval),
            )
        result = command_runner(command, runtime_env, root)
        _write_progress(root, manifest, phase)
        return result

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

        generation = run_long_command(generate_command, "generation")
        report["commands"]["generate"] = _command_record(generation, log_dir, "generate", root)
        if generation.get("returncode") != 0:
            report["status"] = "generation_error"
            report["error"] = {
                "type": "BFCLGenerationError",
                "message": "官方 bfcl generate 返回非零状态",
            }
            report["official_outputs"] = collect_official_outputs(root)
            return report

        evaluation = run_long_command(evaluate_command, "evaluation")
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
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = {
            "type": "KeyboardInterrupt",
            "message": "用户中断了 BFCL 运行",
        }
        try:
            report["official_outputs"] = collect_official_outputs(root)
        except (OSError, ValueError, TypeError):
            pass
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
        try:
            _write_progress(root, manifest, report["status"])
        except OSError:
            pass


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

    run_parser = subparsers.add_parser("run", help="运行所选基准并调用官方 scorer")
    run_parser.add_argument("--bfcl-bin", default=_default_bfcl_bin())
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    selection_group = run_parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--benchmark",
        choices=tuple(BUILTIN_BENCHMARKS),
        default=DEFAULT_BENCHMARK,
        help="选择内置基准；默认 baseline-200",
    )
    selection_group.add_argument(
        "--manifest", type=Path, default=None, help="使用自定义 manifest"
    )
    run_parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    run_parser.add_argument("--output-dir", type=Path, default=None)
    run_parser.add_argument("--temperature", type=float, default=0.001)
    run_parser.add_argument("--num-threads", type=int, default=1)
    run_parser.add_argument("--allow-overwrite", action="store_true")
    run_parser.add_argument("--credential-env", default="DEEPSEEK_API_KEY")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="实时进度刷新秒数；默认 5",
    )

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
        manifest_path=args.manifest or BUILTIN_BENCHMARKS[args.benchmark],
        runs_dir=args.runs_dir,
        output_dir=args.output_dir,
        temperature=args.temperature,
        num_threads=args.num_threads,
        allow_overwrite=args.allow_overwrite,
        credential_env=args.credential_env or None,
        dry_run=args.dry_run,
        progress_interval=args.progress_interval,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in {"completed", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
