"""Read research agent histories only after project/run authorization."""

from __future__ import annotations

import json
from contextlib import closing
import re
import sqlite3

from .runs import RunRepository


def agents(store, project_id, run_id):
    run = RunRepository(store, project_id).get(run_id)
    candidates = [("root", run.get("context_archive"))]
    candidates += [
        (w.get("worker_id"), w.get("context_archive")) for w in run.get("workers", [])
    ]
    path = store.path.resolve().parent / "context.sqlite3"
    if not path.is_file() or path.is_symlink():
        return []
    result = []
    with closing(sqlite3.connect(path)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_context_runs'"
        ).fetchone():
            return []
        for name, archive in candidates:
            if not isinstance(name, str) or not isinstance(archive, dict):
                continue
            scope = archive.get("scope")
            if not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{32}", scope):
                continue
            row = db.execute(
                "SELECT role,status FROM agent_context_runs WHERE scope=? AND outer_run_id=? AND agent_id=?",
                (scope, run_id, f"{run_id}/{name}"),
            ).fetchone()
            if row:
                count = db.execute(
                    "SELECT count(*) FROM agent_context_steps WHERE scope=?", (scope,)
                ).fetchone()[0]
                result.append(
                    {
                        "agent_id": name,
                        "role": row[0],
                        "status": row[1],
                        "step_count": count,
                    }
                )
    return result


def history(store, project_id, run_id, agent_id, *, step_id=None, offset=0, limit=20):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("invalid_history_page")
    permitted = {item["agent_id"] for item in agents(store, project_id, run_id)}
    if agent_id not in permitted:
        raise ValueError("agent_history_not_found")
    run = RunRepository(store, project_id).get(run_id)
    archive = (
        run["context_archive"]
        if agent_id == "root"
        else next(
            w["context_archive"]
            for w in run["workers"]
            if w.get("worker_id") == agent_id
        )
    )
    scope = archive["scope"]
    with closing(
        sqlite3.connect(store.path.resolve().parent / "context.sqlite3")
    ) as db:
        db.row_factory = sqlite3.Row
        if step_id is None:
            count = db.execute(
                "SELECT count(*) FROM agent_context_steps WHERE scope=?", (scope,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT step_id,local_run_id,sequence,status,started_at,finished_at,source_version FROM agent_context_steps WHERE scope=? ORDER BY rowid LIMIT ? OFFSET ?",
                (scope, limit, offset),
            ).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                has_v2 = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_context_summaries_v2'"
                ).fetchone()
                summary = None
                if has_v2:
                    summary = db.execute(
                        "SELECT content,token_count,'exact_local' AS token_count_kind,degraded FROM agent_context_summaries_v2 WHERE scope=? AND step_id=? AND source_version=? AND level='SS' ORDER BY created_at DESC LIMIT 1",
                        (scope, item["step_id"], item["source_version"]),
                    ).fetchone()
                if not summary:
                    summary = db.execute(
                        "SELECT content,token_count,token_count_kind FROM agent_context_summaries WHERE scope=? AND step_id=? AND source_version=? AND level='SS'",
                        (scope, item["step_id"], item["source_version"]),
                    ).fetchone()
                item["ss"] = dict(summary) if summary else None
        else:
            if not db.execute(
                "SELECT 1 FROM agent_context_steps WHERE scope=? AND step_id=?",
                (scope, step_id),
            ).fetchone():
                raise ValueError("step_not_in_agent_context")
            count = db.execute(
                "SELECT count(*) FROM agent_context_events WHERE scope=? AND step_id=?",
                (scope, step_id),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT event FROM agent_context_events WHERE scope=? AND step_id=? ORDER BY sequence LIMIT ? OFFSET ?",
                (scope, step_id, limit, offset),
            ).fetchall()
            items = [json.loads(row[0]) for row in rows]
    return {
        "items": items,
        "offset": offset,
        "total": count,
        "next_offset": offset + len(items) if offset + len(items) < count else None,
    }
