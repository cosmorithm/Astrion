"""将已成功执行的工具观察转换为可保留的证据；不让摘要模型改写原文。

这里是工具与 Memory 之间的适配层。新工具需要明确加入其结果契约，不能仅凭
任意文本里的数字推断事实。失败/被拒绝的调用不进入此层。
"""
from __future__ import annotations

import json
import math


def extract_tool_evidence(name: str, content: str) -> list[dict]:
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    if name in {"search_knowledge", "read_knowledge"}:
        results = data.get("results")
        if not isinstance(results, list):
            return []
        evidence = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if (any(type(item.get(k)) is not int or item[k] < 1
                    for k in ("document_id", "start_line", "end_line"))
                    or item["end_line"] < item["start_line"]
                    or any(not isinstance(item.get(k), str)
                           for k in ("name", "source", "sha256", "excerpt"))
                    or type(item.get("truncated")) is not bool):
                continue
            # 检索分数不是证据的一部分；同一片段跨搜索/按行阅读时可去重。
            evidence.append({"kind": "document", **{k: item[k] for k in (
                "document_id", "name", "source", "sha256", "start_line", "end_line",
                "excerpt", "truncated")}})
        return evidence

    if name == "calculator" and isinstance(data.get("expression"), str):
        value = data.get("result")
        if type(value) in {int, float} and (type(value) is int or math.isfinite(value)):
            return [{"kind": "calculation", "expression": data["expression"], "result": value}]
    if name == "read_file" and all(isinstance(data.get(k), str) for k in ("path", "content")):
        return [{"kind": "file", "path": data["path"], "content": data["content"]}]
    if name == "run_worker" and all(isinstance(data.get(k), str)
                                    for k in ("worker_id", "task", "status", "answer")):
        return [{"kind": "worker_report", **{k: data[k] for k in (
            "worker_id", "task", "status", "answer")}}]
    return []
