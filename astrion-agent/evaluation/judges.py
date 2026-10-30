"""注册赛后裁判。每个裁判接收任务、运行记录和自身配置。"""

from __future__ import annotations

import copy
import json
from time import perf_counter
from typing import Callable

Judge = Callable[[dict, dict, dict], dict]
STATUSES = {"pass", "fail", "unknown", "pending", "error"}


class JudgeRegistry:
    """按名称注册裁判；裁判异常不伪装成 agent 任务失败。"""

    def __init__(self):
        self._judges: dict[str, Judge] = {}

    def register(self, name: str, judge: Judge) -> None:
        if not isinstance(name, str) or not name.strip() or not callable(judge):
            raise ValueError("裁判需要非空名称和可调用函数")
        if name in self._judges:
            raise ValueError(f"裁判名称已注册: {name}")
        self._judges[name] = judge

    def evaluate(self, case: dict, run_record: dict) -> list[dict]:
        results = []
        for spec in case.get("judges", []):
            name = spec.get("name") if isinstance(spec, dict) else None
            started = perf_counter()
            try:
                if not isinstance(name, str) or not name:
                    raise ValueError("裁判配置必须包含名称")
                if name not in self._judges:
                    raise ValueError(f"未注册的裁判: {name}")
                # 每个裁判只收到副本，防止某个裁判修改其他裁判的输入。
                verdict = self._judges[name](copy.deepcopy(case), copy.deepcopy(run_record), copy.deepcopy(spec))
                if not isinstance(verdict, dict) or verdict.get("status") not in STATUSES:
                    raise ValueError("裁判结果必须是包含有效 status 的字典")
                if "reason" in verdict and not isinstance(verdict["reason"], str):
                    raise ValueError("裁判结果的 reason 必须是字符串")
                # 报告要可保存为 JSON；允许裁判附加 score、evidence 等字段。
                json.dumps(verdict, ensure_ascii=False, allow_nan=False)
                result = {**verdict, "judge": name}
            except (Exception, SystemExit) as exc:
                result = {"judge": name, "status": "error",
                          "reason": f"{type(exc).__name__}: {exc}"}
            result["duration_seconds"] = perf_counter() - started
            results.append(result)
        return results


def file_equals(case: dict, run_record: dict, spec: dict) -> dict:
    """比较运行后文件快照和预期文本，忽略首尾空白。"""
    path = spec.get("path")
    expected = spec.get("expected")
    if not isinstance(path, str) or not path or not isinstance(expected, str):
        raise ValueError("file_equals 需要字符串 path 和 expected")

    errors = run_record["artifact_errors"]
    if path in errors:
        return {"status": "unknown", "reason": "无法读取结果文件", "evidence": {"path": path, "error": errors[path]}}
    files = run_record["artifacts"]
    if path not in files:
        return {"status": "fail", "reason": "结果文件不存在", "evidence": {"path": path, "expected": expected}}

    actual = files[path]
    passed = actual.strip() == expected.strip()
    return {
        "status": "pass" if passed else "fail",
        "reason": "结果文件内容符合预期" if passed else "结果文件内容不符合预期",
        "evidence": {"path": path, "expected": expected, "actual": actual},
    }


def default_registry() -> JudgeRegistry:
    registry = JudgeRegistry()
    registry.register("file_equals", file_equals)
    return registry
