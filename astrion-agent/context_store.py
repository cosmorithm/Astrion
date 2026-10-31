"""每个 Agent 独立的上下文档案：不可变内容、原始消息、可恢复视图。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ContextStore:
    def __init__(self, path=None, *, scope=None):
        self.path = str(Path(path).expanduser().resolve()) if path else None
        self.scope = scope or uuid4().hex
        self.records = {}
        self.messages = []
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS context_artifacts (
                        scope TEXT, ref TEXT, record TEXT NOT NULL, PRIMARY KEY(scope, ref));
                    CREATE TABLE IF NOT EXISTS context_messages (
                        scope TEXT, seq INTEGER, message TEXT NOT NULL, PRIMARY KEY(scope, seq));
                    CREATE TABLE IF NOT EXISTS context_checkpoints (
                        scope TEXT PRIMARY KEY, state TEXT NOT NULL);
                ''')
                self.records = {r[0]: json.loads(r[1]) for r in db.execute(
                    'SELECT ref,record FROM context_artifacts WHERE scope=?', (self.scope,))}
                self.messages = [json.loads(r[0]) for r in db.execute(
                    'SELECT message FROM context_messages WHERE scope=? ORDER BY seq', (self.scope,))]
            for ref in self.records:
                self.get(ref)

    def connect(self):
        # context manager 的退出同时提交/回滚并关闭连接。
        class Connection(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        return sqlite3.connect(self.path, timeout=15, factory=Connection)

    @staticmethod
    def reference(record):
        return 'ctx_' + hashlib.sha256(encode(record).encode('utf-8')).hexdigest()

    def put(self, kind, content, metadata=None):
        record = {'kind': kind, 'content': content, 'metadata': metadata or {}}
        ref = self.reference(record)
        if ref not in self.records:
            if self.path:
                with self.connect() as db:
                    db.execute('INSERT OR IGNORE INTO context_artifacts VALUES (?,?,?)',
                               (self.scope, ref, encode(record)))
            self.records[ref] = copy.deepcopy(record)
        return ref

    def get(self, ref):
        if not isinstance(ref, str) or ref not in self.records:
            raise ValueError('当前 Agent 的档案中不存在此引用')
        record = self.records[ref]
        if self.reference(record) != ref:
            raise ValueError('上下文档案校验失败')
        return copy.deepcopy(record)

    def append(self, message):
        seq = len(self.messages) + 1
        encoded = encode(message)
        if self.path:
            with self.connect() as db:
                db.execute('INSERT INTO context_messages VALUES (?,?,?)', (self.scope, seq, encoded))
        self.messages.append(json.loads(encoded))
        return seq

    def snapshot(self):
        return {'scope': self.scope, 'records': copy.deepcopy(self.records),
                'messages': copy.deepcopy(self.messages)}

    def import_snapshot(self, snapshot):
        if snapshot.get('scope') != self.scope:
            raise ValueError('上下文档案 scope 不一致')
        records, messages = snapshot.get('records'), snapshot.get('messages')
        if not isinstance(records, dict) or not isinstance(messages, list):
            raise ValueError('上下文档案格式错误')
        for ref, record in records.items():
            if (not isinstance(record, dict) or set(record) != {'kind', 'content', 'metadata'}
                    or not isinstance(record['content'], str) or not isinstance(record['metadata'], dict)
                    or self.reference(record) != ref):
                raise ValueError('上下文档案内容或哈希错误')
        # JSON 会话可能停在上个完整批次，磁盘日志已追加崩溃前的尾部。
        # 两边共同前缀必须一致；保留较新的尾部，不覆盖或重放它。
        common = min(len(self.messages), len(messages))
        if self.messages[:common] != messages[:common]:
            raise ValueError('拒绝覆盖不一致的原始消息')
        for record in records.values():
            self.put(record['kind'], record['content'], record['metadata'])
        for message in messages[len(self.messages):]:
            self.append(message)

    def checkpoint(self, state):
        if self.path:
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO context_checkpoints VALUES (?,?)', (self.scope, encode(state)))

    def load_checkpoint(self):
        if not self.path:
            raise ValueError('该档案没有持久化路径')
        with self.connect() as db:
            row = db.execute('SELECT state FROM context_checkpoints WHERE scope=?', (self.scope,)).fetchone()
        if row is None:
            raise ValueError('尚无完整轮次的检查点')
        return json.loads(row[0])
