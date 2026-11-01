"""Isolate each actual Astrion run, then evaluate without exposing the gold."""
from __future__ import annotations
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from research.runner import run_research
from research.store import Store
from evaluation.reporting import save_report
from .dataset import load_data, DEFAULT_BASE, digest
from .judges import registry

# No web, external literature, arbitrary code, historical logs or preexisting artifacts.
ALLOWED = {'list_documents', 'document_outline', 'search_knowledge', 'read_knowledge',
           'read_document', 'search_memory', 'finish', 'ask_user', 'update_task_plan',
           'run_worker', 'run_workers'}


def code_version():
    root=Path(__file__).resolve().parents[3]
    try:
        sha=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL,timeout=5).strip()
        dirty=bool(subprocess.check_output(['git','-C',str(root),'status','--porcelain'],text=True,stderr=subprocess.DEVNULL,timeout=5).strip())
        return {'git_commit':sha,'worktree_dirty':dirty,'python':sys.version.split()[0]}
    except (OSError,subprocess.SubprocessError):
        return {'git_commit':None,'worktree_dirty':None,'python':sys.version.split()[0]}


def case_by_id(identifier):
    _, cases = load_data()
    match = [c for c in cases if c['id'] == identifier]
    if len(match) != 1: raise ValueError('unknown_case_id:' + str(identifier))
    return match[0]


def check_base(path):
    corpus, _ = load_data()
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        projects = db.execute('SELECT id FROM projects').fetchall()
        if len(projects) != 1: raise ValueError('unexpected_base_projects')
        project = projects[0]['id']
        if db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]:
            raise ValueError('base_contains_memories')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'artifacts' in tables and db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]:
            raise ValueError('base_contains_artifacts')
        documents = db.execute('SELECT id,name,sha256,original,content,metadata FROM documents WHERE project_id=? ORDER BY id', (project,)).fetchall()
        if len(documents) != 25: raise ValueError('unexpected_base_document_count')
        actual = {d['name']: d for d in documents}
        for paper in corpus['papers']:
            doc = actual.get(paper['name'])
            if doc is None or doc['sha256'] != paper['sha256']:
                raise ValueError('base_source_mismatch:' + paper['id'])
            import hashlib
            if hashlib.sha256(doc['original']).hexdigest() != paper['sha256']:
                raise ValueError('base_blob_mismatch:' + paper['id'])
            meta = json.loads(doc['metadata'])
            if hashlib.sha256(doc['content'].encode('utf-8')).hexdigest() != meta.get('text_sha256'):
                raise ValueError('base_extracted_text_mismatch:' + paper['id'])
        return project, {p['id']: actual[p['name']]['id'] for p in corpus['papers']}
    finally: db.close()


def evaluate(case_id, mode, llm_factory, output, *, base=DEFAULT_BASE, steps=8, worker_steps=6, workers=2, budget_chars=12000):
    started = perf_counter()
    if mode not in {'single','multi'}: raise ValueError('invalid_mode')
    case = case_by_id(case_id)
    project, ids = check_base(base)
    output = Path(output).expanduser().resolve()
    directory = output / 'runs' / f'{case_id}-{mode}-{uuid.uuid4().hex}'
    directory.mkdir(parents=True, exist_ok=False)
    dbpath = directory / 'research.sqlite3'
    # SQLite backup takes a consistent snapshot even if the corpus file is open.
    src = sqlite3.connect(f'file:{Path(base).resolve()}?mode=ro', uri=True)
    dst = sqlite3.connect(dbpath)
    try: src.backup(dst)
    finally: dst.close(); src.close()
    store = Store(dbpath)
    # Each split sees only its own papers; the frozen 25-paper base is untouched.
    _, all_cases = load_data()
    split_ids = {paper for item in all_cases if item['split'] == case['split'] for paper in item['paper_ids']}
    document_ids = sorted(ids[paper] for paper in split_ids)
    placeholders = ','.join('?' for _ in document_ids)
    with store.db:
        store.db.execute(f'DELETE FROM documents WHERE project_id=? AND id NOT IN ({placeholders})',
                         (project, *document_ids))
    prompt_papers = '\n'.join(f"{pid} = 项目文档 D{ids[pid]}" for pid in case['paper_ids'])
    instruction = case.get('continuation', {}).get('initial_task', case['task'])
    question = f"{instruction}\n\n指定论文对应关系：\n{prompt_papers}\n仅使用本项目的考试论文，不能使用外部来源。"
    llm = llm_factory()
    try:
        try:
            record = run_research(store, project, question, llm, max_steps=steps,
                                  max_workers=0 if mode == 'single' else workers,
                                  worker_steps=worker_steps, budget_chars=budget_chars,
                                  tool_allowlist=ALLOWED)
            if case.get('continuation') and record.get('result', {}).get('answer'):
                first = record
                second = run_research(store, project, case['continuation']['follow_up'], llm,
                                      max_steps=steps, max_workers=0 if mode == 'single' else workers,
                                      worker_steps=worker_steps, budget_chars=budget_chars,
                                      continue_run_id=first['run_id'], tool_allowlist=ALLOWED)
                a, b = first['usage'], second['usage']
                totals = {k: a['totals'].get(k, 0) + b['totals'].get(k, 0)
                          for k in set(a['totals']) | set(b['totals'])}
                usage = {'totals': totals, **{k: a[k]+b[k] if a.get(k) is not None and b.get(k) is not None else None
                                            for k in ('calls','unknown_calls')}}
                record = {**second, 'duration_seconds': first['duration_seconds'] + second['duration_seconds'],
                          'usage': usage, 'continuation_stages': [
                              {k:first.get(k) for k in ('run_id','result','usage','duration_seconds')},
                              {k:second.get(k) for k in ('run_id','result','usage','duration_seconds')}]}
        except (Exception, SystemExit) as exc:
            records = list((directory / 'runs' / str(project)).glob('*/result.json'))
            record = json.loads(records[-1].read_text()) if records else {}
            record['result'] = {'status':'error','answer':None,'error_type':type(exc).__name__}
            record.setdefault('duration_seconds', perf_counter()-started)
        judgment_specs = [{'name':'execution'}, {'name':'citation_location'}, {'name':'human_semantic_review'}]
        judgments = registry().evaluate({'id':case_id, 'judges':judgment_specs}, record)
        report = {'kind':'research_eval_case','case_id':case_id,'split':case['split'],'mode':mode,
                  'corpus_id':'agent-test-paper-v1','gold_status':case['gold_status'],
                  'base_database_sha256':digest(Path(base)),
                  'model':getattr(llm,'model',None),
                  'provider_hostname':urlsplit(getattr(llm,'base_url','') or '').hostname,
                  'code_version':code_version(), 'settings':{'steps':steps,'worker_steps':worker_steps,
                    'max_workers':0 if mode == 'single' else workers,'budget_chars':budget_chars,
                    'allowed_tools':sorted(ALLOWED)},
                  'paper_mapping':{p:ids[p] for p in case['paper_ids']},
                  'visible_paper_ids': sorted(split_ids),
                  'run':{k:v for k,v in record.items() if k != 'trace'},'judgments':judgments,
                  'semantic_assessment':'pending','case_outcome':'fail' if any(j['status']=='fail' for j in judgments) else 'pending',
                  'duration_including_setup_seconds':perf_counter()-started, 'workspace':str(directory)}
        path = save_report(report, output / 'reports', filename_prefix=f'{case_id}-{mode}')
        return {**report,'report_path':str(path)}
    finally: store.close()


def summary(reports):
    groups={}
    seen=set()
    for report in reports:
        identity=(report.get('case_id'), report.get('mode'), (report.get('run') or {}).get('run_id'))
        if identity in seen:
            raise ValueError('duplicate_run_report: do not combine a run and its reviewed copy')
        seen.add(identity)
        group=groups.setdefault(report['mode'],{'total':0,'passed':0,'failed':0,'pending':0,
                                             'duration_seconds':0.0,'known_total_tokens':0,'unknown_token_runs':0,
                                             'citation_locations':{'locations_found':0,'invalid_locations':0,'no_citations':0,'unknown':0},
                                             'human_points':{'correct':0,'incorrect':0,'unknown':0},
                                             'model_provisional':{'pass':0,'fail':0,'unknown':0},
                                             'semantic_pending':0,'gold_unreviewed':0})
        group['total']+=1; group[{'pass':'passed','fail':'failed','pending':'pending'}[report['case_outcome']]]+=1
        run=report.get('run') or {}; group['duration_seconds']+=run.get('duration_seconds') or 0
        usage=run.get('usage') or {}; total=(usage.get('totals') or {}).get('total_tokens')
        if total is None or usage.get('unknown_calls') is None or usage.get('unknown_calls')>0:
            group['unknown_token_runs']+=1
        if total is not None: group['known_total_tokens']+=total
        status=(run.get('citation_audit') or {}).get('status')
        group['citation_locations'][status if status in group['citation_locations'] else 'unknown']+=1
        for point in (report.get('human_review') or {}).get('points',[]):
            if point.get('status') in group['human_points']:group['human_points'][point['status']]+=1
        verdict=(report.get('model_review') or {}).get('provisional_outcome')
        if verdict in group['model_provisional']:group['model_provisional'][verdict]+=1
        if report.get('semantic_assessment') != 'human_reviewed': group['semantic_pending']+=1
        if not (report.get('human_review') or {}).get('gold_reviewed',False): group['gold_unreviewed']+=1
    for group in groups.values():
        scored=group['passed']+group['failed']
        group['scored_pass_rate'] = group['passed']/scored if scored and not group['gold_unreviewed'] else None
        group['all_case_pass_rate'] = group['passed']/group['total'] if group['total'] and not group['pending'] and not group['gold_unreviewed'] else None
        group['mean_agent_duration_seconds'] = group['duration_seconds']/group['total'] if group['total'] else None
    return {'kind':'research_eval_suite','groups':groups,'case_reports':[r['report_path'] for r in reports],
            'note':'标准答案未复核时不输出通过率；语义未复核与已知失败分别计数，模型裁判仅作参考。'}
