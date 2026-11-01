"""Exact fetched snapshots, separate from extracted reading text."""

import hashlib
from datetime import datetime, timezone


class Sources:
    def __init__(self, db):
        self.db = db
        db.execute(
            """CREATE TABLE IF NOT EXISTS source_snapshots (
            id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,url TEXT NOT NULL,accessed_at TEXT NOT NULL,
            sha256 TEXT NOT NULL,content_type TEXT NOT NULL,body BLOB NOT NULL,document_id INTEGER)"""
        )
        db.commit()

    def save(self, project, response):
        digest = hashlib.sha256(response["body"]).hexdigest()
        accessed = datetime.now(timezone.utc).isoformat()
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO source_snapshots(project_id,url,accessed_at,sha256,content_type,body) VALUES (?,?,?,?,?,?)",
                (
                    project,
                    response["url"],
                    accessed,
                    digest,
                    response["content_type"],
                    response["body"],
                ),
            )
        return {
            "snapshot_id": cursor.lastrowid,
            "source_url": response["url"],
            "accessed_at": accessed,
            "snapshot_sha256": digest,
        }

    def previous_document(self, project, url, digest):
        r = self.db.execute(
            "SELECT document_id FROM source_snapshots WHERE project_id=? AND url=? AND sha256=? AND document_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (project, url, digest),
        ).fetchone()
        return r[0] if r else None

    def link(self, snapshot, document):
        with self.db:
            self.db.execute(
                "UPDATE source_snapshots SET document_id=? WHERE id=?", (document, snapshot)
            )
