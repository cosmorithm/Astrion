"""Per-run event journal and result; independent of the model context archive."""

import json
import os
from contextlib import contextmanager
import re
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

try:
    import fcntl
except ImportError:  # Fail closed for cleanup and continuation where file locks are unavailable.
    fcntl = None


def deletable_run(result):
    """Only unambiguous failures, including step limits with no report."""
    nested = result.get("result")
    nested = nested if isinstance(nested, dict) else {}
    top_status = result.get("status")
    child_status = nested.get("status")
    if top_status and child_status and top_status != child_status:
        return False
    status = child_status or top_status
    answer = nested.get("answer") or result.get("answer")
    if status == "error":
        return True
    return status == "step_limit" and not answer and not result.get("report_artifact")


@contextmanager
def run_reference_lock(repo, run_id, *, exclusive=False):
    """Cross-process lock: a continued parent cannot be removed until child is saved."""
    if fcntl is None:
        raise ValueError("research_run_lock_unavailable: 当前平台不支持安全的续写锁")
    repo.directory(run_id)  # validate identifier and project path before creating lock
    repo.base.mkdir(parents=True, exist_ok=True)
    locks = repo.base / ".run-locks"
    if locks.is_symlink():
        raise ValueError("research_run_path_denied")
    locks.mkdir(exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(locks / run_id, flags, 0o600)
    except OSError as exc:
        raise ValueError("research_run_lock_unavailable") from exc
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError as exc:
            raise ValueError("research_run_in_use: 研究版本正在被续写，请稍后清理") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
        result["can_delete"] = deletable_run(result)
        return result

    def delete_failed(self, run_id):
        """Remove a failed run only if no saved or in-flight version depends on it."""
        with run_reference_lock(self, run_id, exclusive=True):
            result = self.get(run_id)
            if not deletable_run(result):
                raise ValueError("research_run_not_deletable: 仅允许清理失败或没有报告的步数耗尽记录")
            for path in self.base.glob("research_*/result.json"):
                if path.parent.name == run_id:
                    continue
                try:
                    other = self.get(path.parent.name)
                except (ValueError, OSError, TypeError, AttributeError) as exc:
                    # Broken history makes it impossible to establish whether it depends on this run.
                    if path.is_symlink() or path.parent.is_symlink():
                        raise ValueError("research_history_unreadable: 无法确认是否有后续版本") from exc
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                    except (ValueError, OSError, TypeError):
                        raise ValueError("research_history_unreadable: 无法确认是否有后续版本") from exc
                    # Only skip a record when its explicit, well-formed identity
                    # proves it belongs elsewhere. Missing identity is unknown.
                    if isinstance(raw, dict):
                        other_project = raw.get("project")
                        other_database = raw.get("database_path")
                        if isinstance(other_project, dict) and other_project != self.project:
                            continue
                        if isinstance(other_database, str) and other_database and other_database != self.database:
                            continue
                    raise ValueError("research_history_unreadable: 无法确认是否有后续版本") from exc
                if other.get("parent_run_id") == run_id:
                    raise ValueError("research_run_has_children: 此记录已有后续版本，不能清理")
            directory = self.directory(run_id)
            allowed = {"result.json", "events.jsonl", "recovery.json"}
            entries = list(directory.iterdir())
            if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in entries):
                raise ValueError("research_run_unknown_files: 包含非标准文件，不能自动清理")
            # Atomic rename removes it from history before unlinking known journal files.
            quarantine = self.base / (".deleted_" + uuid4().hex)
            os.rename(directory, quarantine)
            for entry in entries:
                (quarantine / entry.name).unlink()
            quarantine.rmdir()
            return {"deleted": True, "run_id": run_id}

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
            nested = row.get("result") if isinstance(row.get("result"), dict) else {}
            results.append({**{k: row.get(k) for k in (
                "run_id", "parent_run_id", "task", "recorded_at", "duration_seconds", "usage", "report_artifact")},
                "status": nested.get("status") or row.get("status"),
                "can_continue": bool(row.get("context_archive")),
                "can_delete": row["can_delete"],
            })
        results.sort(key=lambda r: (r.get("recorded_at") or "", r["run_id"]), reverse=True)
        return {"runs": results[offset:offset + limit], "total": len(results),
                "next_offset": offset + limit if offset + limit < len(results) else None}
