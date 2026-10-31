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


def terms(text: str) -> set[str]:
    """英文按词、中文按单字及双字切分；仅是可解释的关键词检索。"""
    result = set()
    for token in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower()):
        if '\u3400' <= token[0] <= '\u9fff':
            result.update(token)
            result.update(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.add(token)
    return result


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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

    def add_document(self, project_id: int, path: Path | str) -> int:
        self.project(project_id)
        path = Path(path).expanduser().resolve(strict=True)
        if path.suffix.lower() not in {'.txt', '.md'}:
            raise ValueError('第一版只支持 UTF-8 的 .txt 和 .md 文件')
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError('资料必须是小于等于 1 MB 的普通文件')
        data = path.read_bytes()
        if len(data) > 1_000_000:
            raise ValueError('资料超过 1 MB')
        return self.add_document_text(project_id, path.name, data.decode('utf-8'), str(path))

    def add_document_text(self, project_id: int, name: str, content: str, source: str = '上传') -> int:
        self.project(project_id)
        if not isinstance(name, str) or not name or Path(name).name != name or Path(name).suffix.lower() not in {'.txt', '.md'}:
            raise ValueError('资料名称必须是 .txt 或 .md 文件名')
        if not isinstance(content, str):
            raise ValueError('资料内容必须是文本')
        data = content.encode('utf-8')
        if len(data) > 1_000_000:
            raise ValueError('资料超过 1 MB')
        if not content.strip():
            raise ValueError('资料不能为空')
        with self.db:
            cur = self.db.execute('INSERT INTO documents(project_id,name,source,sha256,content) VALUES (?,?,?,?,?)',
                (project_id, name, source, hashlib.sha256(data).hexdigest(), content))
        return cur.lastrowid

    def documents(self, project_id: int) -> list[dict]:
        self.project(project_id)
        return [dict(row) for row in self.db.execute(
            'SELECT id,name,source,sha256,created_at FROM documents WHERE project_id=? ORDER BY id', (project_id,))]

    def search_documents(self, project_id: int, query: str, limit: int = 5) -> list[dict]:
        self.project(project_id)
        wanted = terms(query)
        if not wanted:
            raise ValueError('检索词不能为空')
        results = []
        for doc in self.db.execute('SELECT id,name,source,sha256,content FROM documents WHERE project_id=? ORDER BY id', (project_id,)):
            lines = doc['content'].splitlines()
            for start in range(0, len(lines), 10):
                excerpt = '\n'.join(lines[start:start + 10])
                score = len(wanted & terms(excerpt))
                if score:
                    # 超长行仅展示前 3000 字符，范围仍指向原始行。
                    results.append({'document_id': doc['id'], 'name': doc['name'], 'source': doc['source'],
                        'sha256': doc['sha256'], 'start_line': start + 1, 'end_line': min(start + 10, len(lines)),
                        'excerpt': excerpt[:3000], 'truncated': len(excerpt) > 3000, 'score': score})
        results.sort(key=lambda r: (-r['score'], r['document_id'], r['start_line']))
        return results[:limit]

    def read_document(self, project_id: int, document_id: int, start_line: int, end_line: int) -> dict:
        """按原始行号读取相邻证据；所有读取都绑定当前项目。"""
        self.project(project_id)
        if any(type(n) is not int for n in (document_id, start_line, end_line)):
            raise ValueError('文档 ID 和行号必须是整数')
        if start_line < 1 or end_line < start_line or end_line - start_line + 1 > 80:
            raise ValueError('行号从 1 开始，结束行不能小于起始行，每次最多读取 80 行')
        doc = self.db.execute(
            'SELECT id,name,source,sha256,content FROM documents WHERE project_id=? AND id=?',
            (project_id, document_id)).fetchone()
        if doc is None:
            raise ValueError('当前项目中不存在此文档')
        lines = doc['content'].splitlines()
        if start_line > len(lines):
            raise ValueError(f'起始行超过文档总行数 {len(lines)}')
        end_line = min(end_line, len(lines))
        excerpt = '\n'.join(lines[start_line - 1:end_line])
        return {'document_id': doc['id'], 'name': doc['name'], 'source': doc['source'],
                'sha256': doc['sha256'], 'start_line': start_line, 'end_line': end_line,
                'total_lines': len(lines), 'excerpt': excerpt[:6000], 'truncated': len(excerpt) > 6000}

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
