"""Replay the same real research prefix under isolated context strategies.

The prefix is reconstructed from archived messages and source events through a
completed Step. Later messages, artifacts, summaries, and events are excluded.
Both groups receive the same original prefix, task, tools, and follow-up.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4

import agent
from context_history import AgentHistory, copy_history
from context_store import ContextStore, encode
from context_tokenizer import load_token_counter
from context_tools import CONTEXT_SPECS
from memory import Memory
from research.citations import audit_citations, observed_ranges
from research.prompts import FINAL_STEP_PROMPT
from research.store import Store
from tools.research import build_tools
from evaluation.reporting import save_report
from evaluation.research.runner import code_version
from .runner import SINGLE_ALLOWED

FOLLOW_UP = ('请基于目前研究过程继续完成原定报告，特别复核第一篇和第四篇指定论文的组织方式与依据。'
             '请遵守此前仍有效的所有用户要求；无法验证的内容明确说明。')


def backup(source, destination):
    src = sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro', uri=True)
    dst = sqlite3.connect(destination)
    try: src.backup(dst)
    finally: src.close(); dst.close()


def prepare_prefix(report, workspace, prefix_steps):
    archive_info = report['run']['context_archive']
    source_dir = Path(report['workspace'])
    backup(archive_info['path'], workspace/'context.sqlite3')
    backup(source_dir/'research.sqlite3', workspace/'research.sqlite3')
    with sqlite3.connect(workspace/'research.sqlite3') as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Do not inherit a report artifact created after the frozen prefix.
        if 'artifacts' in tables:
            db.execute('DELETE FROM artifacts')
        if 'memories' in tables:
            db.execute('DELETE FROM memories')
    old = ContextStore(workspace/'context.sqlite3', scope=archive_info['scope'])
    source_memory = Memory.from_archive(old.path, old.scope)
    with old.connect() as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM agent_context_steps WHERE scope=? AND status='completed' ORDER BY rowid", (old.scope,))]
    if not rows:
        raise ValueError('source_has_no_completed_steps')
    selected = rows[:min(prefix_steps,len(rows))]
    cutoff = selected[-1]['last_seq']
    messages = copy.deepcopy(old.messages[:cutoff])
    refs = set()
    for message in messages:
        if message.get('_ref'): refs.add(message['_ref'])
        refs.update(message.get('_evidence_refs') or [])
    records = {ref:copy.deepcopy(old.records[ref]) for ref in refs}
    target = ContextStore(old.path)
    target.import_snapshot({'scope':target.scope,'records':records,'messages':messages})
    copy_history(old,target)
    step_ids = [row['step_id'] for row in selected]
    placeholders = ','.join('?' for _ in step_ids)
    with target.connect() as db:
        db.execute(f'DELETE FROM agent_context_events WHERE scope=? AND (step_id IS NULL OR step_id NOT IN ({placeholders}))',
                   (target.scope,*step_ids))
        db.execute(f'DELETE FROM agent_context_snapshots WHERE scope=? AND (step_id IS NULL OR step_id NOT IN ({placeholders}))',
                   (target.scope,*step_ids))
        db.execute(f'DELETE FROM agent_context_steps WHERE scope=? AND step_id NOT IN ({placeholders})',
                   (target.scope,*step_ids))
        for table in ('agent_context_summaries','agent_context_summaries_v2','agent_context_builds'):
            db.execute(f'DELETE FROM {table} WHERE scope=?',(target.scope,))
        # The cloned file must not retain another scope containing future events.
        for table in ('context_artifacts','context_messages','context_checkpoints',
                      'agent_context_runs','agent_context_steps','agent_context_events',
                      'agent_context_snapshots','agent_context_summaries',
                      'agent_context_summaries_v2','agent_context_builds'):
            db.execute(f'DELETE FROM {table} WHERE scope<>?',(target.scope,))
    memory = Memory(source_memory.system_prompt,budget_chars=source_memory.budget_chars,
                    keep_recent=source_memory.keep_recent,archive=target,
                    tool_limit_chars=source_memory.tool_limit_chars,
                    pin_budget_chars=source_memory.pin_budget_chars)
    memory._history = [{**copy.deepcopy(msg),'_seq':i} for i,msg in enumerate(messages,1)]
    if memory.check():
        raise ValueError('source_prefix_has_invalid_tool_protocol')
    prefix_history = AgentHistory(target, 'prefix-audit-' + uuid4().hex)
    prefix_events = [{'role':'root','event':event} for step_id in step_ids
                     for event in prefix_history.step(step_id)['events']]
    return memory, len(selected), cutoff, observed_ranges(prefix_events)


def fingerprint(memory, api_tools):
    with memory.archive.connect() as db:
        events = [json.loads(row[0]) for row in db.execute(
            'SELECT event FROM agent_context_events WHERE scope=? ORDER BY sequence', (memory.archive.scope,))]
        steps = [list(row) for row in db.execute(
            'SELECT step_id,sequence,status,first_seq,last_seq,source_version FROM agent_context_steps WHERE scope=? ORDER BY rowid',
            (memory.archive.scope,))]
    value={'messages':memory.for_model(), 'records':memory.archive.records,
           'events':events, 'steps':steps, 'tools':api_tools}
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def prepare_only(report, output, strategies, *, prefix_steps, max_steps, input_budget_tokens):
    model=report['model']
    origin = 'deepseek:api.deepseek.com' if report.get('provider_hostname')=='api.deepseek.com' else None
    rows=[]
    for strategy in strategies:
        workspace = Path(output)/'runs'/('prepared-'+strategy+'-'+uuid4().hex)
        workspace.mkdir(parents=True,exist_ok=False)
        memory, count, cutoff, ranges=prepare_prefix(report,workspace,prefix_steps)
        memory.system_prompt += f'\n本次固定历史续答最多 {max_steps} 轮；以本阶段预算为准。'
        memory.reasoning_origin=origin
        memory.reasoning_synthetic_empty=origin is not None
        store=Store(workspace/'research.sqlite3')
        try:
            specs,_=build_tools(store,report['run']['project']['id'],allowlist=SINGLE_ALLOWED)
            api_tools=[s.as_api_tool() for s in [*specs,*CONTEXT_SPECS]]
            rows.append({'strategy':strategy,'source_prefix_steps':count,'source_cutoff_message_seq':cutoff,
                         'prefix_sha256':fingerprint(memory,api_tools),
                         'raw_local_tokens':load_token_counter(model).count_request(memory.for_model(),api_tools),
                         'input_budget_tokens':input_budget_tokens,'prior_observed_ranges':len(ranges),
                         'workspace':str(workspace)})
        finally:store.close()
    if len(rows)==2 and rows[0]['prefix_sha256'] != rows[1]['prefix_sha256']:
        raise RuntimeError('fixed_prefix_mismatch')
    (Path(output)/'prepared.json').write_text(json.dumps({'groups':rows,'status':'prepared_not_run'},ensure_ascii=False,indent=2))
    return rows


def run_group(source_report, strategy, output, llm_factory, *, prefix_steps=10,
              max_steps=12, input_budget_tokens=12000):
    workspace = Path(output)/'runs'/('fixed-'+strategy+'-'+uuid4().hex)
    workspace.mkdir(parents=True,exist_ok=False)
    memory, actual_steps, cutoff, prior_ranges = prepare_prefix(source_report, workspace, prefix_steps)
    # Original prompt describes the source run; explicitly state the separate
    # continuation budget so both models are not misled about remaining steps.
    memory.system_prompt += f'\n本次固定历史续答最多 {max_steps} 轮；以本阶段预算为准。'
    store = Store(workspace/'research.sqlite3')
    project = source_report['run']['project']['id']
    specs, impl = build_tools(store,project,allowlist=SINGLE_ALLOWED)
    llm = llm_factory()
    memory.reasoning_origin = getattr(llm,'reasoning_origin',None)
    memory.reasoning_synthetic_empty = bool(getattr(llm,'reasoning_synthetic_empty',False))
    counter = load_token_counter(getattr(llm,'model',None))
    api_tools = [s.as_api_tool() for s in [*specs, *CONTEXT_SPECS]]
    raw_count = counter.count_request(memory.for_model(), api_tools)
    prefix_hash = fingerprint(memory, api_tools)
    task = next(m['content'] for m in memory._history if m['role']=='user')
    events = []
    previous = os.environ.get('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS')
    os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS'] = str(input_budget_tokens)
    started = perf_counter()
    try:
        try:
            result = agent.run(task,llm,memory,max_steps=max_steps,resume=True,user_reply=FOLLOW_UP,
                               context_strategy=strategy,tool_specs=specs,tool_implementations=impl,
                               final_step_prompt=FINAL_STEP_PROMPT,event_sink=events.append,capture_model_input=True)
        except (Exception,SystemExit) as exc:
            result = {'status':'error','answer':None,'error':{'type':type(exc).__name__,'message':str(exc)[:1000]}}
        payload = {
            'kind':'long_research_fixed_history', 'source_case_id':source_report['case_id'],
            'source_report':source_report['report_path'], 'source_strategy':source_report['strategy'],
            'source_prefix_steps':actual_steps,'source_cutoff_message_seq':cutoff,
            'prefix_note':'Common raw source prefix; later messages and artifacts excluded; pins and summaries not inherited.',
            'strategy':strategy,'raw_local_tokens':raw_count,'prefix_sha256':prefix_hash,
            'input_budget_tokens':input_budget_tokens,'max_steps':max_steps,
            'follow_up':FOLLOW_UP,'result':result,'usage':{'totals':llm.usage_totals,
                'calls':llm.usage_calls,'unknown_calls':llm.usage_unknown},
            'duration_seconds':perf_counter()-started,'trace':events,'workspace':str(workspace),
            'code_version':code_version(),'semantic_assessment':'pending_human_review',
        }
        # The trace uses the core event shape; wrap for research citation auditing.
        wrapped = [{'role':'root','event':e} for e in events]
        payload['citation_audit'] = audit_citations(result.get('answer'),wrapped,prior_ranges)
        payload['prefix_observed_ranges'] = prior_ranges
        purposes = [e['payload'].get('purpose') for e in events if e['type'] == 'model.call.started']
        payload['metrics'] = {
            'decision_calls':purposes.count('decision'),
            'ss_calls':purposes.count('context_ss'),
            'planner_calls':purposes.count('context_plan') + purposes.count('context_plan_phase'),
            'compactions_applied':sum(e['type']=='memory.compaction.finished' and bool(e['payload'].get('compacted')) for e in events),
            'history_reads':sum(e['type']=='tool.call.finished' and e['payload'].get('name') in
                                {'search_step_history','read_step_history','search_evidence','read_evidence'} for e in events),
        }
        if strategy == 'legacy' and (payload['metrics']['ss_calls'] or payload['metrics']['planner_calls']):
            raise RuntimeError('strategy_isolation_violation')
        payload['citation_note'] = 'Prefix and follow-up observed ranges are credited; semantic support is unreviewed.'
        path=save_report(payload,Path(output)/'reports',filename_prefix='fixed-'+strategy)
        return {**payload,'report_path':str(path)}
    finally:
        if previous is None: os.environ.pop('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS',None)
        else: os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS']=previous
        store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--prefix-steps',type=int,default=10)
    parser.add_argument('--steps',type=int,default=12)
    parser.add_argument('--strategy',choices=('both','legacy','ss'),default='both')
    parser.add_argument('--prepare-only',action='store_true',help='Offline: verify isolated identical prefixes without calling a model')
    parser.add_argument('--input-budget-tokens',type=int,default=12000)
    args=parser.parse_args()
    if args.prefix_steps < 1 or args.steps < 1 or args.input_budget_tokens < 1:
        parser.error('steps and budget must be positive')
    report=json.loads(args.source_report.read_text(encoding='utf-8'))
    report['report_path']=str(args.source_report.resolve())
    strategies=('legacy','ss') if args.strategy=='both' else (args.strategy,)
    out=args.output.resolve()
    if args.prepare_only:
        out.mkdir(parents=True,exist_ok=False)
        rows=prepare_only(report,out,strategies,prefix_steps=args.prefix_steps,max_steps=args.steps,
                          input_budget_tokens=args.input_budget_tokens)
        print('固定历史已准备，未调用模型:',out/'prepared.json')
        print('前缀哈希:',*[r['prefix_sha256'] for r in rows])
        return
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key=agent.first_nonempty(os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
    if not key:parser.error('missing model API key')
    endpoint=os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com'
    model=os.environ.get('AGENT_MODEL') or 'deepseek-flash'
    out.mkdir(parents=True,exist_ok=False)
    groups=[]
    for strategy in strategies:
        print('开始固定历史回放',strategy,flush=True)
        group=run_group(report,strategy,out,lambda:agent.ChatLLM(key,endpoint,model),
                        prefix_steps=args.prefix_steps,max_steps=args.steps,input_budget_tokens=args.input_budget_tokens)
        groups.append({k:group[k] for k in ('strategy','source_prefix_steps','raw_local_tokens','prefix_sha256','input_budget_tokens','max_steps','result','usage','duration_seconds','metrics','report_path')})
        if len(groups)==2 and groups[0]['prefix_sha256'] != groups[1]['prefix_sha256']:
            raise RuntimeError('fixed_prefix_mismatch')
        (out/'suite.json').write_text(json.dumps({'groups':groups},ensure_ascii=False,indent=2))
        print('完成固定历史回放',strategy,group['result']['status'],flush=True)
    print('总报告:',out/'suite.json')

if __name__ == '__main__': main()
