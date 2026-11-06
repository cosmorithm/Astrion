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
from .commands import artifacts as artifact_commands
from .config import DEFAULT_STEPS, DEFAULT_WORKERS
from .runs import RunRepository


def main():
    p = argparse.ArgumentParser(description="Astrion Research：本地资料、显式记忆与研究 Agent")
    p.add_argument(
        "--db", type=Path, default=Path("research_data/astrion.sqlite3"), help="SQLite 数据库路径"
    )
    sub = p.add_subparsers(dest="command", required=True)
    project = sub.add_parser("project-add", help="创建科研项目")
    project.add_argument("name")
    sub.add_parser("projects", help="列出项目")
    doc = sub.add_parser("document-add", help="导入 TXT/MD/PDF")
    doc.add_argument("--project", type=int, required=True)
    doc.add_argument("path", type=Path)
    doc.add_argument("--ocr", action="store_true")
    mem = sub.add_parser("memory-add", help="显式保存长期记忆")
    mem.add_argument("--scope", choices=["conversation", "project"], required=True)
    mem.add_argument("--project", type=int)
    mem.add_argument("content")
    memories = sub.add_parser("memories", help="查看有效记忆")
    memories.add_argument("--project", type=int)
    forget = sub.add_parser("memory-forget", help="使记忆失效（保留原文，不是彻底删除）")
    forget.add_argument("id", type=int)
    search = sub.add_parser("search", help="关键词检索当前项目资料")
    search.add_argument("--project", type=int, required=True)
    search.add_argument("query")
    search.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    search.add_argument("--offset", type=int, default=0)
    run = sub.add_parser("run", help="运行研究 Agent（调用真实模型）")
    run.add_argument("--project", type=int, required=True)
    run.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    run.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    run.add_argument("--delegation-mode", choices=("baseline", "structured"), default="baseline",
                     help="委派方式；structured 会先调用一次独立路由器，可能增加成本")
    run.add_argument("--worker-steps", type=int, default=DEFAULT_STEPS)
    run.add_argument("--context-budget-chars", type=int, help="软压缩触发阈值（字符，非 token）")
    run.add_argument("--continue-run", help="继承指定历史运行的上下文、证据与待办，另存新版本")
    run.add_argument("--trace", type=Path, help="保存运行事实 JSON，目标文件必须不存在")
    run.add_argument("task")
    runs = sub.add_parser("runs", help="查看研究历史和可续写的运行 ID")
    runs.add_argument("--project", type=int, required=True)
    runs.add_argument("--offset", type=int, default=0)
    runs.add_argument("--limit", type=int, default=20)
    docs = sub.add_parser("documents")
    docs.add_argument("--project", type=int, required=True)
    outline = sub.add_parser("outline")
    outline.add_argument("--project", type=int, required=True)
    outline.add_argument("document_id", type=int)
    outline.add_argument("--offset", type=int, default=0)
    read = sub.add_parser("read")
    read.add_argument("--project", type=int, required=True)
    read.add_argument("document_id", type=int)
    for option, default in [
        ("page", 0),
        ("section", 0),
        ("start-line", 1),
        ("end-line", 0),
        ("offset", 0),
        ("limit", 4000),
    ]:
        read.add_argument("--" + option, type=int, default=default)
    lit = sub.add_parser("literature")
    lit.add_argument("--project", type=int, required=True)
    lit.add_argument("query")
    lit.add_argument("--cursor", default="*")
    doi = sub.add_parser("doi")
    doi.add_argument("--project", type=int, required=True)
    doi.add_argument("doi")
    doi.add_argument("--offset", type=int, default=0)
    web = sub.add_parser("source-add")
    web.add_argument("--project", type=int, required=True)
    web.add_argument("url")
    artifact_commands.register(sub)
    args = p.parse_args()
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    try:
        store = Store(args.db)
        try:
            if args.command in artifact_commands.COMMANDS:
                result = artifact_commands.dispatch(store, args)
            elif args.command == "project-add":
                result = {"project_id": store.create_project(args.name)}
            elif args.command == "projects":
                result = store.projects()
            elif args.command == "runs":
                result = RunRepository(store, args.project).listing(args.offset, args.limit)
            elif args.command == "document-add":
                result = {"document_id": store.add_document(args.project, args.path, ocr=args.ocr)}
            elif args.command == "memory-add":
                result = {"memory_id": store.add_memory(args.scope, args.content, args.project)}
            elif args.command == "memories":
                result = store.memories(args.project)
            elif args.command == "memory-forget":
                result = {"forgotten": store.forget(args.id)}
            elif args.command == "search":
                result = store.search(args.project, args.query, mode=args.mode, offset=args.offset)
            elif args.command == "documents":
                result = store.documents(args.project)
            elif args.command == "outline":
                result = store.document_store.outline(args.project, args.document_id, args.offset)
            elif args.command == "read":
                result = store.document_store.read_part(
                    args.project,
                    args.document_id,
                    page=args.page,
                    section=args.section,
                    start_line=args.start_line,
                    end_line=args.end_line,
                    offset=args.offset,
                    limit=args.limit,
                )
            elif args.command in {"literature", "doi", "source-add"}:
                from tools.external import ExternalTools

                store.project(args.project)
                external = ExternalTools(store, args.project)
                raw = (
                    external.search_literature(args.query, args.cursor)
                    if args.command == "literature"
                    else (
                        external.doi_metadata(args.doi, args.offset)
                        if args.command == "doi"
                        else external.read_webpage(args.url)
                    )
                )
                result = json.loads(raw)
            else:
                if args.trace and args.trace.exists():
                    raise ValueError("trace 文件已存在，请选择新路径")
                agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
                key = agent.first_nonempty(
                    os.environ.get("AGENT_API_KEY"),
                    os.environ.get("DEEPSEEK_API_KEY"),
                    os.environ.get("OPENAI_API_KEY"),
                )
                if not key:
                    raise ValueError("未找到模型 API key")
                llm = agent.ChatLLM(
                    key,
                    os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com",
                    os.environ.get("AGENT_MODEL") or "deepseek-chat",
                )
                result = run_research(
                    store,
                    args.project,
                    args.task,
                    llm,
                    max_steps=args.steps,
                    max_workers=args.workers,
                    worker_steps=args.worker_steps,
                    budget_chars=args.context_budget_chars,
                    continue_run_id=args.continue_run,
                    delegation_mode=args.delegation_mode,
                )
                if args.trace:
                    with args.trace.open("x", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                result = {k: v for k, v in result.items() if k != "trace"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
    except (ValueError, OSError, UnicodeError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
