"""Run isolated, paired long research tasks over the fixed local paper corpus."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from time import perf_counter
from urllib.parse import urlsplit

import agent
from evaluation.reporting import save_report
from evaluation.research.dataset import DEFAULT_BASE, digest, load_data
from evaluation.research.judges import citations, execution, human_review
from evaluation.research.runner import ALLOWED, check_base, code_version
from research.runner import run_research
from research.store import Store

CASE_PATH = Path(__file__).with_name('cases.json')
SINGLE_ALLOWED = ALLOWED - {'run_worker', 'run_workers'}
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAMES = ('agent.py','memory.py','context_construction.py','events.py',
                'research/runner.py','evaluation/long_research/runner.py')
# Capture at module import, before paid calls; later worktree edits must not
# silently change the recorded identity of code already loaded in memory.
SOURCE_SHA256 = {name:digest(SOURCE_ROOT/name) for name in SOURCE_NAMES}


def cases():
    data = json.loads(CASE_PATH.read_text(encoding='utf-8'))
    corpus, source = load_data()
    known = {p['id'] for p in corpus['papers']}
    references = {c['id']: c for c in source}
    seen = set()
    for case in data['cases']:
        if case['id'] in seen or set(case['papers']) - known:
            raise ValueError('invalid_long_research_case')
        seen.add(case['id'])
        covered = {p for key in case['source_case_ids'] for p in references[key]['paper_ids']}
        if covered != set(case['papers']):
            raise ValueError('paper_rubric_mismatch:' + case['id'])
        if not case['initial'].strip() or any(not int(k) > 0 or not v.strip() for k,v in case['updates'].items()):
            raise ValueError('invalid_long_research_prompt')
    return data['cases']


def metrics(record, scheduled, paper_mapping):
    trace = record.get('trace') or []
    events = [x['event'] for x in trace if x.get('role') == 'root']
    updates = [e for e in events if e['type'] == 'user.message.added']
    decisions = [e for e in events if e['type'] == 'model.call.started' and e['payload'].get('purpose') == 'decision']
    sources = [e for e in events if e['type'] == 'model.call.started']
    reasons = [e['payload'].get('purpose') for e in sources]
    results = [e for e in events if e['type'] == 'memory.compaction.finished']
    retrieved = {r[0] for r in record.get('observed_ranges') or []}
    audits = (record.get('citation_audit') or {}).get('checks') or []
    last_request = ((decisions[-1]['payload'].get('request_snapshot') or {}).get('messages') or []) if decisions else []
    present_updates = [step for step,text in scheduled.items()
                       if any(m.get('role')=='user' and m.get('content')==text for m in last_request)]
    return {
        'decision_calls': len(decisions),
        'scheduled_updates': len(scheduled), 'delivered_updates': len(updates),
        'delivered_steps': [e['step'] for e in updates],
        'updates_in_last_decision_input': sorted(present_updates),
        'all_updates_delivered': len(updates) == len(scheduled),
        'papers_with_observed_ranges': [p for p,d in paper_mapping.items() if d in retrieved],
        'paper_observation_note': 'Complete, untruncated returned source ranges only; not proof of full paper reading or correct claims.',
        'citations_total': len(audits), 'citations_in_observed_ranges': sum(bool(c.get('in_retrieved_range')) for c in audits),
        'citation_semantic_support': 'pending',
        'compaction_attempts': len(results), 'compactions_applied': sum(bool(e['payload'].get('compacted')) for e in results),
        'ss_rebuilds_applied': sum(bool((e['payload'].get('rebuild') or {}).get('status') == 'applied') for e in results),
        'ss_calls': reasons.count('context_ss'), 'planner_calls': reasons.count('context_plan') + reasons.count('context_plan_phase'),
        'history_reads': sum(e['type']=='tool.call.finished' and e['payload'].get('name') in {'read_step_history','search_step_history','read_evidence','search_evidence'} for e in events),
        'local_peak_attempted_request_tokens': max((e['payload'].get('input',{}).get('local_input_tokens') or 0 for e in sources), default=0),
        'long_process_observed': len(decisions) >= 10,
        'compaction_pressure_observed': bool(results),
    }


def run_case(case, strategy, llm_factory, output, *, base=DEFAULT_BASE, max_steps=30,
             budget_chars=16000, input_budget_tokens=12000):
    if strategy not in {'legacy', 'ss'}:
        raise ValueError('invalid_context_strategy')
    project, ids = check_base(base)
    corpus, _ = load_data()
    by_id = {p['id']:p for p in corpus['papers']}
    mapping = {p:ids[p] for p in case['papers']}
    directory = Path(output).resolve() / 'runs' / f"{case['id']}-{strategy}-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / 'research.sqlite3'
    source = sqlite3.connect(f'file:{Path(base).resolve()}?mode=ro', uri=True)
    target = sqlite3.connect(database)
    try:
        source.backup(target)
    finally:
        source.close(); target.close()
    store = Store(database)
    allowed = sorted(mapping.values())
    marks = ','.join('?' for _ in allowed)
    with store.db:
        store.db.execute(f'DELETE FROM documents WHERE project_id=? AND id NOT IN ({marks})', (project,*allowed))
    papers = '\n'.join(f"{p} = D{mapping[p]} = {by_id[p]['name']}" for p in case['papers'])
    task = case['initial'] + '\n\n指定论文（仅用这些项目资料，不使用外部来源）：\n' + papers
    schedule = {int(k):v for k,v in case['updates'].items()}
    llm = llm_factory()
    started = perf_counter()
    prior = os.environ.get('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS')
    os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS'] = str(input_budget_tokens)
    error = None
    try:
        try:
            record = run_research(store, project, task, llm, max_steps=max_steps, max_workers=0,
                                  worker_steps=6, budget_chars=budget_chars, tool_allowlist=SINGLE_ALLOWED,
                                  context_strategy=strategy, scheduled_user_updates=schedule)
        except (Exception, SystemExit) as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
            paths = list((directory/'runs'/str(project)).glob('*/result.json'))
            record = json.loads(paths[-1].read_text()) if paths else {}
            record['result'] = {'status':'error', 'answer':None, 'error':error}
            journal = paths[-1].with_name('events.jsonl') if paths else None
            record['trace'] = [json.loads(line) for line in journal.read_text().splitlines()] if journal and journal.is_file() else []
        stats = metrics(record, schedule, mapping)
        if strategy == 'legacy' and (stats['ss_calls'] or stats['planner_calls'] or stats['ss_rebuilds_applied']):
            raise RuntimeError('strategy_isolation_violation')
        judgments = [execution({},record,{}), citations({},record,{}), human_review({},record,{})]
        report = {
            'kind':'long_research_eval', 'case_id':case['id'], 'strategy':strategy,
            'source_sha256': SOURCE_SHA256,
            'gold_status':'provisional_needs_human_review',
            'cases_sha256':digest(CASE_PATH), 'base_database_sha256':digest(Path(base)),
            'code_version':code_version(), 'model':getattr(llm,'model',None),
            'provider_hostname':urlsplit(getattr(llm,'base_url','') or '').hostname,
            'settings':{'max_steps':max_steps,'max_workers':0,'budget_chars':budget_chars,
                        'input_budget_tokens':input_budget_tokens,'allowed_tools':sorted(SINGLE_ALLOWED),
                        'scheduled_user_updates':schedule},
            'paper_mapping':mapping, 'source_case_ids':case['source_case_ids'],
            'run':{k:v for k,v in record.items() if k!='trace'}, 'metrics':stats,
            'judgments':judgments, 'semantic_assessment':'pending_human_review',
            'case_outcome':('fail' if any(j['status']=='fail' for j in judgments) else 'pending_human_review'), 'error':error,
            'usage_note':'Provider-reported totals; failed requests and transport retries may have unknown usage.',
            'duration_seconds':perf_counter()-started,'workspace':str(directory),
        }
        path = save_report(report, Path(output)/'reports', filename_prefix=case['id']+'-'+strategy)
        return {**report,'report_path':str(path)}
    finally:
        if prior is None: os.environ.pop('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS',None)
        else: os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS'] = prior
        store.close()
