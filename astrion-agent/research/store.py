"""SQLite 资料库与显式长期记忆；研究模型没有写入数据库的权限。"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL, source TEXT NOT NULL, sha256 TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('conversation','project')),
    project_id INTEGER REFERENCES projects(id), content TEXT NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','forgotten')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK((scope='conversation' AND project_id IS NULL) OR (scope='project' AND project_id IS NOT NULL))
);
"""


from .retrieval.search import terms, search
from .storage.documents import Documents, migrate


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        migrate(self.db)
        self.document_store = Documents(self.db)

    def close(self):
        self.db.close()

    def create_project(self, name: str) -> int:
        if not name.strip():
            raise ValueError('项目名不能为空')
        with self.db:
            cur = self.db.execute('INSERT INTO projects(name) VALUES (?)', (name.strip(),))
        return cur.lastrowid

    def project(self, project_id: int):
        row = self.db.execute('SELECT id, name FROM projects WHERE id=?', (project_id,)).fetchone()
        if row is None:
            raise ValueError(f'项目 {project_id} 不存在')
        return dict(row)

    def projects(self):
        return [dict(row) for row in self.db.execute('SELECT id,name,created_at FROM projects ORDER BY id')]

    def add_document(self, project_id, path, *, ocr=False):
        self.project(project_id)
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 20_000_000:
            raise ValueError('资料必须是小于等于 20 MB 的普通文件')
        return self.import_document(project_id, path.name, path.read_bytes(), str(path), ocr=ocr)

    def import_document(self, project_id, name, data, source='上传', *, ocr=False, extra=None):
        self.project(project_id)
        return self.document_store.add(project_id, name, data, source, ocr=ocr, extra=extra)

    def add_document_text(self, project_id, name, content, source='上传'):
        if not isinstance(content, str): raise ValueError('资料内容必须是文本')
        if Path(name).suffix.lower() not in {'.txt', '.md'}: raise ValueError('资料名称必须是 .txt 或 .md 文件名')
        return self.import_document(project_id,name,content.encode('utf-8'),source)

    def documents(self, project_id):
        self.project(project_id)
        return self.document_store.listing(project_id, limit=10000)

    def search_documents(self, project_id, query, limit=5):
        # Legacy list-returning API remains lexical and deterministic.
        return search(self, project_id, query, limit=limit, mode='keyword')['results']

    def search(self, project_id, query, **options):
        return search(self, project_id, query, **options)

    def read_document(self, project_id, document_id, start_line, end_line):
        return self.document_store.read_lines(project_id,document_id,start_line,end_line)

    def add_memory(self, scope: str, content: str, project_id: int | None = None) -> int:
        if scope not in ('conversation', 'project') or not content.strip():
            raise ValueError('记忆类型不合法或内容为空')
        if scope == 'project':
            if project_id is None:
                raise ValueError('项目记忆需要项目 ID')
            self.project(project_id)
        elif project_id is not None:
            raise ValueError('对话记忆不能绑定项目')
        with self.db:
            cur = self.db.execute('INSERT INTO memories(scope,project_id,content,source) VALUES (?,?,?,?)',
                                  (scope, project_id, content.strip(), 'user_explicit'))
        return cur.lastrowid

    def memories(self, project_id: int | None = None) -> list[dict]:
        if project_id is not None:
            self.project(project_id)
        return [dict(row) for row in self.db.execute(
            "SELECT id,scope,project_id,content,source,created_at FROM memories "
            "WHERE status='active' AND (scope='conversation' OR (scope='project' AND project_id=?)) ORDER BY id", (project_id,))]

    def search_memories(self, project_id: int, query: str, limit: int = 5) -> list[dict]:
        wanted = terms(query)
        if not wanted:
            raise ValueError('检索词不能为空')
        matches = []
        for row in self.memories(project_id):
            score = len(wanted & terms(row['content']))
            if score:
                matches.append({**row, 'score': score})
        return sorted(matches, key=lambda x: (-x['score'], x['id']))[:limit]

    def forget(self, memory_id: int) -> bool:
        # 软删除保留内容，用于后续审计；不是彻底擦除。
        with self.db:
            cur = self.db.execute("UPDATE memories SET status='forgotten' WHERE id=? AND status='active'", (memory_id,))
        return cur.rowcount == 1
