"""Per-run event journal and result; independent of the model context archive."""

import json
import re
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4


class RunLog:
    def __init__(self, store, project):
        self.database = str(store.path.resolve())
        self.lock = Lock()
        self.run_id = "research_" + uuid4().hex
        self.directory = store.path.parent / "runs" / str(project) / self.run_id
        self.directory.mkdir(parents=True)
        self.path = self.directory / "events.jsonl"

    def append(self, item):
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")

    def finish(self, result):
        payload = {k: v for k, v in result.items() if k != "trace"}
        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
        payload["database_path"] = self.database
        (self.directory / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class RunRepository:
    """Read immutable run versions in one project; legacy results remain readable."""

    def __init__(self, store, project_id):
        self.project = store.project(project_id)
        self.base = store.path.resolve().parent / "runs" / str(project_id)
        self.database = str(store.path.resolve())

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"research_[a-f0-9]{32}", run_id):
            raise ValueError("invalid_research_run_id")
        path = self.base / run_id
        if self.base.is_symlink() or path.is_symlink() or path.resolve().parent != self.base.resolve():
            raise ValueError("research_run_path_denied")
        return path

    def get(self, run_id):
        path = self.directory(run_id) / "result.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("research_run_not_found_in_project")
        result = json.loads(path.read_text(encoding="utf-8"))
        # Old files lack a database identity. Match both project ID and name for
        # that legacy format (test databases can share the same filesystem parent).
        if (result.get("project") != self.project
                or result.get("database_path", self.database) != self.database):
            raise ValueError("research_run_not_found_in_project")
        if result.get("run_id") != run_id:
            raise ValueError("research_run_identity_mismatch")
        # Optional recovery metadata for legacy failed runs whose result omitted
        # the checkpoint. The original result and event journal stay untouched.
        recovery = path.with_name("recovery.json")
        if recovery.is_file() and not recovery.is_symlink() and not result.get("context_archive"):
            data = json.loads(recovery.read_text(encoding="utf-8"))
            if data.get("run_id") == run_id and data.get("database_path") == self.database:
                result.update({k: data[k] for k in ("context_archive", "observed_ranges", "workers") if k in data})
        return result

    def trace(self, run_id):
        self.get(run_id)
        path = self.directory(run_id) / "events.jsonl"
        if path.is_symlink():
            raise ValueError("research_run_path_denied")
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        return []

    def events_page(self, run_id, offset=0, limit=10):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid_pagination: offset >= 0, limit 1–20")
        self.get(run_id)
        path = self.directory(run_id) / 'events.jsonl'
        if path.is_symlink():
            raise ValueError('research_run_path_denied')
        events = []
        if path.is_file():
            with path.open(encoding='utf-8') as handle:
                for index, line in enumerate(handle):
                    if index < offset:
                        continue
                    if len(events) == limit:
                        return {'events': events, 'next_offset': offset + limit}
                    events.append(json.loads(line))
        return {'events': events, 'next_offset': None}

    def listing(self, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid_pagination: offset >= 0, limit 1–50")
        results = []
        for path in self.base.glob("research_*/result.json"):
            try:
                row = self.get(path.parent.name)
            except (ValueError, OSError):
                continue
            results.append({**{k: row.get(k) for k in (
                "run_id", "parent_run_id", "task", "recorded_at", "duration_seconds", "usage", "report_artifact")},
                "status": row.get("result", {}).get("status", row.get("status")),
                "can_continue": bool(row.get("context_archive")),
            })
        results.sort(key=lambda r: (r.get("recorded_at") or "", r["run_id"]), reverse=True)
        return {"runs": results[offset:offset + limit], "total": len(results),
                "next_offset": offset + limit if offset + limit < len(results) else None}
