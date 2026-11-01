"""Immutable project files; callers select IDs, never host filesystem paths."""

from __future__ import annotations
from ..errors import ResearchError

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from uuid import uuid4

MAX_FILE = 8_000_000
TEXT_SUFFIXES = {".csv", ".json", ".txt", ".md", ".py", ".tsv"}
SUFFIXES = TEXT_SUFFIXES | {".png", ".jpg", ".jpeg", ".pdf"}


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 180
        or name in {".", ".."}
        or any(c in name for c in "/\\:")
        or any(ord(c) < 32 for c in name)
        or Path(name).suffix.lower() not in SUFFIXES
    ):
        raise ResearchError(
            "invalid_artifact_name: use a simple CSV/JSON/TXT/MD/PY/TSV/PNG/JPG/PDF filename"
        )
    return name


class Artifacts:
    def __init__(self, store):
        self.store = store
        self.base = store.path.resolve().parent / "projects"
        store.db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            media_type TEXT NOT NULL, provenance TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        store.db.commit()

    def _path(self, project, identifier, name):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ResearchError("invalid_artifact_id")
        path = self.base / str(project) / "artifacts" / identifier / safe_name(name)
        # Container code cannot write to this tree; reject pre-existing host symlinks too.
        for part in [self.base, *list(path.relative_to(self.base).parents)[:-1]]:
            candidate = part if part.is_absolute() else self.base / part
            if candidate.is_symlink():
                raise ResearchError("artifact_symlink_denied")
        if path.is_symlink():
            raise ResearchError("artifact_symlink_denied")
        return path

    def put(self, project, name, data, *, provenance=None):
        self.store.project(project)
        safe_name(name)
        if not isinstance(data, bytes) or len(data) > MAX_FILE:
            raise ResearchError("artifact_size_limit: maximum 8 MB")
        identifier = uuid4().hex
        path = self._path(project, identifier, name)
        path.parent.mkdir(parents=True, exist_ok=False)
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        sha = hashlib.sha256(data).hexdigest()
        try:
            with self.store.db:
                # Reserve quota under the same write transaction as insertion.
                self.store.db.execute("BEGIN IMMEDIATE")
                size, count = self.store.db.execute(
                    "SELECT COALESCE(SUM(size),0),COUNT(*) FROM artifacts WHERE project_id=?", (project,)
                ).fetchone()
                if size + len(data) > 256_000_000 or count >= 2000:
                    raise ResearchError("project_artifact_quota: maximum 256 MB or 2000 files")
                with path.open("xb") as handle:
                    handle.write(data)
                self.store.db.execute(
                    "INSERT INTO artifacts(id,project_id,name,size,sha256,media_type,provenance) VALUES (?,?,?,?,?,?,?)",
                    (
                        identifier,
                        project,
                        name,
                        len(data),
                        sha,
                        media,
                        json.dumps(provenance or {}, ensure_ascii=False),
                    ),
                )
        except BaseException:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
            raise
        return self.get(project, identifier)

    def get(self, project, identifier):
        self.store.project(project)
        row = self.store.db.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND id=?", (project, identifier)
        ).fetchone()
        if row is None:
            raise ResearchError("artifact_not_found_in_project")
        item = dict(row)
        item["provenance"] = json.loads(item["provenance"])
        return item

    def listing(self, project, offset=0, limit=20):
        self.store.project(project)
        if offset < 0 or not 1 <= limit <= 30:
            raise ResearchError("invalid_pagination")
        rows = self.store.db.execute(
            "SELECT id FROM artifacts WHERE project_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (project, limit + 1, offset),
        ).fetchall()
        return {
            "artifacts": [self.get(project, row["id"]) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def bytes(self, project, identifier):
        item = self.get(project, identifier)
        path = self._path(project, identifier, item["name"])
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ResearchError("artifact_file_missing_or_oversize")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ResearchError("artifact_integrity_error")
        return data

    def read(self, project, identifier, offset=0, limit=4000):
        item = self.get(project, identifier)
        if offset < 0 or not 1 <= limit <= 6000:
            raise ResearchError("invalid_pagination: offset>=0; limit must be 1..6000; follow next_offset")
        if Path(item["name"]).suffix.lower() not in TEXT_SUFFIXES:
            raise ResearchError("binary_artifact: use download")
        text = self.bytes(project, identifier).decode("utf-8")
        return {
            **item,
            "content": text[offset : offset + limit],
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }
