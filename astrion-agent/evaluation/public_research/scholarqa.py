"""Run Astrion for one ScholarQA-CS2 task without exposing its rubric.

This is a custom-tool interface: Astrion's Crossref/web tools, not the official
Asta MCP toolset. Official scoring is a separate AstaBench/Inspect operation.
"""
from __future__ import annotations
import contextlib
import json
import uuid
from pathlib import Path
from evaluation.research.runner import code_version
from research.runner import run_research
from research.store import Store


def execute(question, factory, output, *, steps=10, workers=3, worker_steps=6, budget_chars=12000):
    if not isinstance(question,str) or not question.strip(): raise ValueError('question required')
    path=Path(output)/('scholarqa-'+uuid.uuid4().hex)
    path.mkdir(parents=True,exist_ok=False)
    store=Store(path/'research.sqlite3')
    project=store.create_project('ScholarQA-CS2 isolated')
    llm=factory()
    try:
        # The official input contains question and required response schema, never the gold rubric.
        with (path/'console.log').open('w',encoding='utf-8') as log, contextlib.redirect_stdout(log):
            record=run_research(store,project,question,llm,max_steps=steps,max_workers=workers,
                                worker_steps=worker_steps,budget_chars=budget_chars)
        report={'benchmark':'ScholarQA-CS2','mode':'custom Astrion tools; not Asta standard toolset',
                'score':'pending: official AstaBench scorer required','code_version':code_version(),
                'settings':{'steps':steps,'workers':workers,'worker_steps':worker_steps,'budget_chars':budget_chars},
                'result':{k:v for k,v in record.items() if k!='trace'}}
        (path/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return path,report
    finally: store.close()
