"""Agent-scoped, append-only execution history and context derivations.

This uses the existing ContextStore SQLite file. Every query is scoped; Step
payloads and request snapshots remain independent from compacted model views.
"""

from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from context_store import encode


def now():
    return datetime.now(timezone.utc).isoformat()


class AgentHistory:
    def __init__(self, archive, local_run_id, metadata=None):
        self.archive = archive
        self.scope = archive.scope
        self.local_run_id = local_run_id
        self.metadata = dict(metadata or {})
        self.active = None
        if archive.path:
            with archive.connect() as db:
                db.executescript("""
CREATE TABLE IF NOT EXISTS agent_context_runs (
 scope TEXT NOT NULL, local_run_id TEXT NOT NULL, outer_run_id TEXT, agent_id TEXT NOT NULL,
 parent_agent_id TEXT, role TEXT NOT NULL, task TEXT, started_at TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(scope,local_run_id));
CREATE INDEX IF NOT EXISTS agent_context_runs_outer ON agent_context_runs(outer_run_id,role);
CREATE TABLE IF NOT EXISTS agent_context_steps (
 scope TEXT NOT NULL, step_id TEXT NOT NULL, local_run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, first_seq INTEGER, last_seq INTEGER,
 source_version TEXT, PRIMARY KEY(scope,step_id), UNIQUE(scope,local_run_id,sequence));
CREATE TABLE IF NOT EXISTS agent_context_events (
 scope TEXT NOT NULL, event_id TEXT NOT NULL, local_run_id TEXT NOT NULL, step_id TEXT,
 sequence INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(scope,event_id));
CREATE INDEX IF NOT EXISTS agent_context_events_step ON agent_context_events(scope,step_id,sequence);
CREATE TABLE IF NOT EXISTS agent_context_snapshots (
 scope TEXT NOT NULL, snapshot_id TEXT NOT NULL, local_run_id TEXT NOT NULL, step_id TEXT,
 purpose TEXT, snapshot TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope,snapshot_id));
CREATE TABLE IF NOT EXISTS agent_context_summaries (
 scope TEXT NOT NULL, summary_id TEXT NOT NULL, step_id TEXT NOT NULL, source_version TEXT NOT NULL,
 level TEXT NOT NULL, content TEXT NOT NULL, token_count INTEGER, token_count_kind TEXT NOT NULL,
 model TEXT, usage TEXT, created_at TEXT NOT NULL, PRIMARY KEY(scope,summary_id),
 UNIQUE(scope,step_id,source_version,level));
CREATE TABLE IF NOT EXISTS agent_context_builds (
 scope TEXT NOT NULL, build_id TEXT NOT NULL, local_run_id TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(scope,build_id));
""")
                db.execute(
                    "INSERT INTO agent_context_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        self.scope,
                        local_run_id,
                        self.metadata.get("outer_run_id"),
                        self.metadata.get("agent_id") or "agent_" + self.scope,
                        self.metadata.get("parent_agent_id"),
                        self.metadata.get("role") or "agent",
                        self.metadata.get("task"),
                        now(),
                        "running",
                    ),
                )

    def begin_step(self, sequence):
        if not self.archive.path:
            return None
        if self.active is not None:
            raise ValueError("previous_step_not_closed")
        step_id = f"{self.local_run_id}:step:{sequence}"
        with self.archive.connect() as db:
            db.execute(
                "INSERT INTO agent_context_steps(scope,step_id,local_run_id,sequence,status,started_at,first_seq) VALUES (?,?,?,?,?,?,?)",
                (
                    self.scope,
                    step_id,
                    self.local_run_id,
                    sequence,
                    "running",
                    now(),
                    len(self.archive.messages) + 1,
                ),
            )
        self.active = step_id
        return step_id

    def record(self, event):
        if not self.archive.path:
            return
        event_type = event["type"]
        payload = event["payload"]
        step_id = self.active
        # Context operations before a task step are not themselves task steps.
        if event_type.startswith("memory.compaction.") or (
            event_type.startswith("model.call.")
            and payload.get("purpose")
            in {"compaction", "context_ss", "context_summary", "context_plan"}
        ):
            step_id = None
        with self.archive.connect() as db:
            db.execute(
                "INSERT INTO agent_context_events VALUES (?,?,?,?,?,?)",
                (
                    self.scope,
                    event["event_id"],
                    self.local_run_id,
                    step_id,
                    event["sequence"],
                    encode(event),
                ),
            )
            if event_type == "model.call.started" and isinstance(
                payload.get("request_snapshot"), dict
            ):
                db.execute(
                    "INSERT INTO agent_context_snapshots VALUES (?,?,?,?,?,?,?)",
                    (
                        self.scope,
                        payload["model_call_id"],
                        self.local_run_id,
                        step_id,
                        payload.get("purpose"),
                        encode(payload["request_snapshot"]),
                        event["occurred_at"],
                    ),
                )
            if event_type == "run.finished":
                db.execute(
                    "UPDATE agent_context_runs SET status=? WHERE scope=? AND local_run_id=?",
                    (payload.get("status", "finished"), self.scope, self.local_run_id),
                )
        if event_type == "model.call.failed" and step_id is not None:
            with self.archive.connect() as db:
                db.execute(
                    "UPDATE agent_context_runs SET status=? WHERE scope=? AND local_run_id=?",
                    ("failed", self.scope, self.local_run_id),
                )
            self.end_step("failed")
        elif event_type == "run.finished" and self.active is not None:
            self.end_step("completed")

    def end_step(self, status="completed"):
        if not self.archive.path or self.active is None:
            return
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid_step_status")
        with self.archive.connect() as db:
            rows = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT event FROM agent_context_events WHERE scope=? AND step_id=? ORDER BY sequence",
                    (self.scope, self.active),
                )
            ]
            version = hashlib.sha256(encode(rows).encode()).hexdigest()
            db.execute(
                "UPDATE agent_context_steps SET status=?,finished_at=?,last_seq=?,source_version=? WHERE scope=? AND step_id=? AND status=?",
                (
                    status,
                    now(),
                    len(self.archive.messages),
                    version,
                    self.scope,
                    self.active,
                    "running",
                ),
            )
        self.active = None

    def fail_run(self):
        """Close a recoverable failure without inventing a completed step."""
        if not self.archive.path:
            return
        self.end_step("failed")
        with self.archive.connect() as db:
            db.execute(
                "UPDATE agent_context_runs SET status=? WHERE scope=? AND local_run_id=?",
                ("failed", self.scope, self.local_run_id),
            )

    def steps(self):
        if not self.archive.path:
            return []
        with self.archive.connect() as db:
            db.row_factory = __import__("sqlite3").Row
            rows = db.execute(
                "SELECT * FROM agent_context_steps WHERE scope=? ORDER BY rowid",
                (self.scope,),
            ).fetchall()
            return [dict(r) for r in rows]

    def step(self, step_id):
        if not self.archive.path:
            raise ValueError("history_unavailable")
        with self.archive.connect() as db:
            db.row_factory = __import__("sqlite3").Row
            row = db.execute(
                "SELECT * FROM agent_context_steps WHERE scope=? AND step_id=?",
                (self.scope, step_id),
            ).fetchone()
            if row is None:
                raise ValueError("step_not_in_agent_context")
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT event FROM agent_context_events WHERE scope=? AND step_id=? ORDER BY sequence",
                    (self.scope, step_id),
                )
            ]
        return {**dict(row), "events": events}

    def get_summary(self, step_id, source_version, level):
        if not self.archive.path:
            return None
        with self.archive.connect() as db:
            db.row_factory = __import__("sqlite3").Row
            row = db.execute(
                "SELECT * FROM agent_context_summaries WHERE scope=? AND step_id=? AND source_version=? AND level=?",
                (self.scope, step_id, source_version, level),
            ).fetchone()
            return dict(row) if row else None

    def save_summary(
        self,
        step_id,
        source_version,
        level,
        content,
        *,
        model=None,
        usage=None,
        token_count=None,
        token_count_kind="unavailable",
    ):
        if (
            level not in ("SS", "S", "M", "L")
            or not isinstance(content, str)
            or not content.strip()
        ):
            raise ValueError("invalid_summary")
        if not self.archive.path:
            raise ValueError("history_unavailable")
        with self.archive.connect() as db:
            row = db.execute(
                "SELECT source_version,status FROM agent_context_steps WHERE scope=? AND step_id=?",
                (self.scope, step_id),
            ).fetchone()
            if (
                not row
                or row[0] != source_version
                or row[1] not in ("completed", "failed", "interrupted")
            ):
                raise ValueError("invalid_summary_source")
            db.execute(
                "INSERT OR IGNORE INTO agent_context_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.scope,
                    "summary_" + uuid4().hex,
                    step_id,
                    source_version,
                    level,
                    content,
                    token_count,
                    token_count_kind,
                    model,
                    encode(usage) if usage is not None else None,
                    now(),
                ),
            )
        return self.get_summary(step_id, source_version, level)

    def record_build(self, payload):
        if not self.archive.path:
            return None
        build_id = "build_" + uuid4().hex
        with self.archive.connect() as db:
            db.execute(
                "INSERT INTO agent_context_builds VALUES (?,?,?,?,?)",
                (self.scope, build_id, self.local_run_id, encode(payload), now()),
            )
        return build_id


def copy_history(source, target):
    """Fork a continuation's history to its new scope; never share a live view."""
    if not source.path or source.path != target.path:
        raise ValueError("history_fork_requires_same_database")
    tables = (
        "agent_context_runs",
        "agent_context_steps",
        "agent_context_events",
        "agent_context_snapshots",
        "agent_context_summaries",
        "agent_context_builds",
    )
    with source.connect() as db:
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in tables:
            if table not in existing:
                continue  # Older checkpoints predate the history schema.
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            rest = ",".join(column for column in columns if column != "scope")
            db.execute(
                f"INSERT INTO {table} (scope,{rest}) SELECT ?,{rest} FROM {table} WHERE scope=?",
                (target.scope, source.scope),
            )
