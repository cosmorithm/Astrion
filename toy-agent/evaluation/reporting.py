"""把每次评测结果保存为独立 JSON 文件。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "eval_reports"


def save_report(report: dict, report_dir: Path | str | None = None) -> Path:
    """保存完整报告，使用唯一文件名和独占创建，避免覆盖旧结果。"""
    directory = Path(report_dir if report_dir is not None else DEFAULT_REPORT_DIR).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    case_id = re.sub(r"[^A-Za-z0-9_-]+", "_", report["case_id"]).strip("_-")[:48] or "case"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    for _ in range(5):
        path = directory / f"{case_id}_{timestamp}_{uuid.uuid4().hex}.json"
        # 先序列化，确保不可序列化的报告不会留下半成品文件。
        payload = json.dumps({**report, "report_path": str(path), "report_error": None},
                             ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        try:
            output = path.open("x", encoding="utf-8")
        except FileExistsError:
            continue
        try:
            with output:
                output.write(payload)
        except (OSError, ValueError):
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return path
    raise FileExistsError("无法生成不重名的报告文件")
