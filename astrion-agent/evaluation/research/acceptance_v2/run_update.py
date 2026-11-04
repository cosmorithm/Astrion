"""Run R21 in a fresh database; answer keys never enter the Agent prompt."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from evaluation.reporting import save_report
from evaluation.research.runner import ALLOWED, check_base, code_version
from research.runner import run_research
from research.store import Store
from .__main__ import HERE, validate


def run_update(llm_factory, output, *, source, base, steps=10, budget_chars=12000, toolset='claims'):
    if type(steps) is not int or steps < 3:
        raise ValueError('R21 requires at least three decision steps')
    if toolset not in {'baseline', 'claims'}:
        raise ValueError('unknown_toolset')
    verification = validate(source, base)
    if not verification['valid']:
        raise ValueError('acceptance_source_validation_failed')
    data = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
    case = next(c for c in data['cases'] if c['id'] == 'R21')
    project, ids = check_base(base)
    directory = Path(output).resolve() / 'runs' / ('R21-' + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    dbpath = directory / 'research.sqlite3'
    src = sqlite3.connect(f'file:{Path(base).resolve()}?mode=ro', uri=True)
    dst = sqlite3.connect(dbpath)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    store = Store(dbpath)
    try:
        with store.db:
            store.db.execute('DELETE FROM documents WHERE project_id=? AND id<>?',
                             (project, ids['P25']))
        prompt = f"{case['task']}\n\n指定论文 P25 = 项目文档 D{ids['P25']}。仅使用此项目资料。"
        updates = {int(k): v for k, v in case['scheduled_user_updates'].items()}
        llm = llm_factory()
        allowed = ALLOWED | ({'record_claim', 'list_claims'} if toolset == 'claims' else set())
        result = run_research(store, project, prompt, llm, max_steps=steps,
                              max_workers=0, worker_steps=6, budget_chars=budget_chars,
                              tool_allowlist=allowed,
                              scheduled_user_updates=updates)
        delivered = [x['event']['step'] for x in result['trace']
                     if x['event']['type'] == 'user.message.added']
        report = {'kind': 'research_acceptance_v2', 'case_id': 'R21',
                  'gold_status': 'assistant_reviewed_human_pending',
                  'semantic_assessment': 'pending', 'case_outcome': 'pending',
                  'update_delivery': {'scheduled_steps': sorted(updates), 'delivered_steps': delivered,
                                      'all_delivered': sorted(updates) == sorted(delivered)},
                  'note': '未送达的未来要求不得算作违反；引用语义及金标准仍需人工复核。',
                  'code_version': code_version(),
                  'cases_sha256': hashlib.sha256((HERE / 'cases.json').read_bytes()).hexdigest(),
                  'model': getattr(llm, 'model', None),
                  'settings': {'steps': steps, 'max_workers': 0, 'budget_chars': budget_chars, 'toolset': toolset,
                               'allowed_tools': sorted(allowed),
                               'added_to_v1_allowlist': sorted(allowed - ALLOWED)},
                  'workspace': str(directory), 'run': {k: v for k, v in result.items() if k != 'trace'}}
        report['report_path'] = str(save_report(report, Path(output) / 'reports', filename_prefix='R21'))
        return report
    finally:
        store.close()
