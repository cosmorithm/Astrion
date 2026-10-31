"""本地研究命令。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import agent
from .runner import run_research
from .store import Store


def main():
    p = argparse.ArgumentParser(description='Astrion Research：本地资料、显式记忆与研究 Agent')
    p.add_argument('--db', type=Path, default=Path('research_data/astrion.sqlite3'), help='SQLite 数据库路径')
    sub = p.add_subparsers(dest='command', required=True)
    project = sub.add_parser('project-add', help='创建科研项目'); project.add_argument('name')
    sub.add_parser('projects', help='列出项目')
    doc = sub.add_parser('document-add', help='导入 UTF-8 文本或 Markdown'); doc.add_argument('--project', type=int, required=True); doc.add_argument('path', type=Path)
    mem = sub.add_parser('memory-add', help='显式保存长期记忆'); mem.add_argument('--scope', choices=['conversation', 'project'], required=True); mem.add_argument('--project', type=int); mem.add_argument('content')
    memories = sub.add_parser('memories', help='查看有效记忆'); memories.add_argument('--project', type=int)
    forget = sub.add_parser('memory-forget', help='使记忆失效（保留原文，不是彻底删除）'); forget.add_argument('id', type=int)
    search = sub.add_parser('search', help='关键词检索当前项目资料'); search.add_argument('--project', type=int, required=True); search.add_argument('query')
    run = sub.add_parser('run', help='运行研究 Agent（调用真实模型）'); run.add_argument('--project', type=int, required=True); run.add_argument('--steps', type=int, default=10); run.add_argument('--workers', type=int, default=2); run.add_argument('--worker-steps', type=int, default=4); run.add_argument('--trace', type=Path, help='保存运行事实 JSON，目标文件必须不存在'); run.add_argument('task')
    args = p.parse_args()
    try:
        store = Store(args.db)
        try:
            if args.command == 'project-add': result = {'project_id': store.create_project(args.name)}
            elif args.command == 'projects': result = store.projects()
            elif args.command == 'document-add': result = {'document_id': store.add_document(args.project, args.path)}
            elif args.command == 'memory-add': result = {'memory_id': store.add_memory(args.scope, args.content, args.project)}
            elif args.command == 'memories': result = store.memories(args.project)
            elif args.command == 'memory-forget': result = {'forgotten': store.forget(args.id)}
            elif args.command == 'search': result = store.search_documents(args.project, args.query)
            else:
                if args.trace and args.trace.exists():
                    raise ValueError('trace 文件已存在，请选择新路径')
                agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
                key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'), os.environ.get('DEEPSEEK_API_KEY'), os.environ.get('OPENAI_API_KEY'))
                if not key: raise ValueError('未找到模型 API key')
                llm = agent.ChatLLM(key, os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com', os.environ.get('AGENT_MODEL') or 'deepseek-chat')
                result = run_research(store, args.project, args.task, llm, max_steps=args.steps, max_workers=args.workers, worker_steps=args.worker_steps)
                if args.trace:
                    with args.trace.open('x', encoding='utf-8') as f: json.dump(result, f, ensure_ascii=False, indent=2)
                result = {k: v for k, v in result.items() if k != 'trace'}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
    except (ValueError, OSError, UnicodeError, sqlite3.Error) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
