"""Documents, immutable source bytes, location metadata and pagination."""

import hashlib
import json
from pathlib import Path
from research.ingestion import extract_document


def migrate(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(documents)")}
    if "metadata" not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    if "original" not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN original BLOB")
    db.execute(
        "CREATE TABLE IF NOT EXISTS embedding_cache (document_id INTEGER, backend TEXT, chunk_key TEXT, vector TEXT NOT NULL, PRIMARY KEY(document_id,backend,chunk_key))"
    )
    db.commit()


class Documents:
    def __init__(self, db):
        self.db = db

    def get(self, project, document):
        row = self.db.execute(
            "SELECT * FROM documents WHERE project_id=? AND id=?", (project, document)
        ).fetchone()
        if row is None:
            raise ValueError("当前项目中不存在此文档")
        return dict(row)

    def count(self, project):
        return self.db.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (project,)
        ).fetchone()[0]

    def add(self, project, name, data, source="上传", *, ocr=False, extra=None):
        if not name or Path(name).name != name:
            raise ValueError("资料名称不能包含目录")
        text, metadata = extract_document(name, data, ocr=ocr)
        metadata.update(extra or {})
        metadata["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO documents(project_id,name,source,sha256,content,metadata,original) VALUES (?,?,?,?,?,?,?)",
                (
                    project,
                    name,
                    source,
                    hashlib.sha256(data).hexdigest(),
                    text,
                    json.dumps(metadata, ensure_ascii=False),
                    data,
                ),
            )
        return cursor.lastrowid

    def listing(self, project, offset=0, limit=100):
        result = []
        for r in self.db.execute(
            "SELECT id,name,source,sha256,created_at,metadata FROM documents WHERE project_id=? ORDER BY id LIMIT ? OFFSET ?",
            (project, limit, offset),
        ):
            item = dict(r)
            meta = json.loads(item.pop("metadata"))
            item["metadata"] = {
                k: meta[k]
                for k in ("format", "warnings", "content_kind", "source_url", "accessed_at")
                if k in meta
            }
            item["metadata"]["page_count"] = len(meta.get("pages", []))
            result.append(item)
        return result

    def locations(self, row, start, end):
        meta = json.loads(row.get("metadata") or "{}")
        pages = [
            p for p in meta.get("pages", []) if p["start_line"] <= end and p["end_line"] >= start
        ]
        return {
            "pages": pages,
            "sections": [
                s
                for s in meta.get("sections", [])
                if s["start_line"] <= end and s["end_line"] >= start
            ],
            "warnings": meta.get("warnings", []),
            "content_kind": meta.get("content_kind", "full_text"),
            "source_url": meta.get("source_url"),
            "accessed_at": meta.get("accessed_at"),
        }

    def observation(self, row, start, end, excerpt, *, truncated=False):
        return {
            "document_id": row["id"],
            "name": row["name"],
            "source": row["source"],
            "sha256": row["sha256"],
            "start_line": start,
            "end_line": end,
            "excerpt": excerpt,
            "truncated": truncated,
            **self.locations(row, start, end),
        }

    def read_lines(self, project, document, start, end):
        if (
            any(type(n) is not int for n in (document, start, end))
            or start < 1
            or end < start
            or end - start + 1 > 80
        ):
            raise ValueError(
                f"invalid_line_range: 含首尾最多 80 行；请求 {start}..{end}，请分页（结束行最多 start_line+79）"
            )
        row = self.get(project, document)
        lines = row["content"].splitlines()
        if start > len(lines):
            raise ValueError(f"起始行超过文档总行数 {len(lines)}")
        end = min(end, len(lines))
        text = "\n".join(lines[start - 1 : end])
        result = self.observation(row, start, end, text[:6000], truncated=len(text) > 6000)
        return {
            **result,
            "total_lines": len(lines),
            "next_line": end + 1 if end < len(lines) else None,
            "continuation": (
                {"tool": "read_document", "offset": 6000, "start_line": start, "end_line": end}
                if len(text) > 6000
                else None
            ),
        }

    def read_part(
        self,
        project,
        document,
        *,
        page=0,
        section=0,
        start_line=1,
        end_line=0,
        offset=0,
        limit=4000,
    ):
        if (
            any(
                type(n) is not int or n < 0
                for n in (page, section, start_line, end_line, offset, limit)
            )
            or not 1 <= limit <= 6000
        ):
            raise ValueError("invalid_read_parameters")
        if page and section:
            raise ValueError("page 和 section 只能选择一个")
        row = self.get(project, document)
        meta = json.loads(row["metadata"])
        lines = row["content"].splitlines()
        if page or section:
            items = meta.get("pages" if page else "sections", [])
            field = "page" if page else "id"
            target = page or section
            selected = next((x for x in items if x[field] == target), None)
            if not selected:
                raise ValueError("page_or_section_not_found")
            start_line, end_line = selected["start_line"], selected["end_line"]
        end_line = end_line or len(lines)
        if not 1 <= start_line <= end_line <= len(lines):
            raise ValueError("invalid_line_range")
        text = "\n".join(lines[start_line - 1 : end_line])
        if offset > len(text):
            raise ValueError("offset_out_of_range")
        stop = min(len(text), offset + limit)
        excerpt = text[offset:stop]
        actual_start = start_line + text[:offset].count("\n")
        actual_end = actual_start + excerpt.rstrip("\n").count("\n")
        partial_line = (offset > 0 and text[offset - 1] != "\n") or (
            stop < len(text) and text[stop - 1 : stop] != "\n" and text[stop : stop + 1] != "\n"
        )
        result = self.observation(row, actual_start, actual_end, excerpt, truncated=partial_line)
        return {
            **result,
            "offset": offset,
            "total_chars": len(text),
            "next_offset": stop if stop < len(text) else None,
            "selection": {
                "page": page,
                "section": section,
                "start_line": start_line,
                "end_line": end_line,
            },
            "complete_selection": offset == 0 and stop == len(text),
        }

    def outline(self, project, document, offset=0, limit=30):
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("invalid_pagination")
        row = self.get(project, document)
        meta = json.loads(row["metadata"])
        items = meta.get("sections", []) or meta.get("pages", [])
        return {
            "document_id": document,
            "name": row["name"],
            "items": items[offset : offset + limit],
            "warnings": meta.get("warnings", []),
            "page_count": len(meta.get("pages", [])),
            "total_lines": len(row["content"].splitlines()),
            "next_offset": offset + limit if offset + limit < len(items) else None,
        }
