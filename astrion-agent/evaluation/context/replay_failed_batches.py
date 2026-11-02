"""Offline replay of the *last batch* in a saved live-recall report.

Uses the archived original assistant/tool batch and visible user inputs, plus a
synthetic older step. This isolates token-pressure recovery; it does not replay
all original steps, judge the answer, or call a model.
"""
import argparse
import contextlib
import io
import os
import copy,json,tempfile
import agent
from pathlib import Path
from context_construction import ContextConstructor
from context_history import AgentHistory
from context_store import ContextStore
from context_tokenizer import load_token_counter
from memory import Memory

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', required=True, type=Path, help='Saved live-recall JSON')
parser.add_argument('--strategy', choices=('legacy', 'ss', 'both'), default='both')
parser.add_argument('--live', action='store_true', help='Send one real model request per selected batch; incurs cost')
args = parser.parse_args()
source = args.report
if args.live:
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'), os.environ.get('DEEPSEEK_API_KEY'), os.environ.get('OPENAI_API_KEY'))
    if not key:
        parser.error('missing model API key')
report=json.loads(source.read_text())
counter=load_token_counter('deepseek-flash')
for group in report['groups']:
    if args.strategy != 'both' and group['strategy'] != args.strategy:
        continue
    req=[e['payload']['request_snapshot'] for e in group['trace'] if e['type']=='model.call.started' and e['payload'].get('purpose')=='decision'][-1]
    visible=req['messages']; archive=group['archive_snapshot']; raw=archive['messages']; limit=group['budget']['local_budget_tokens']
    last=next(m for m in reversed(visible) if m.get('tool_calls'))
    last_ids={c['id'] for c in last['tool_calls']}
    start=next(i for i,m in enumerate(raw) if {c['id'] for c in m.get('tool_calls',[])} == last_ids)
    end=start+1
    while end<len(raw) and raw[end].get('role')=='tool' and raw[end].get('tool_call_id') in last_ids:
        end+=1
    assert end-start==len(last_ids)+1
    with tempfile.TemporaryDirectory() as temp:
        store=ContextStore(Path(temp)/'replay.sqlite',scope=archive['scope']);store.import_snapshot(archive)
        memory=Memory(visible[0]['content'],budget_chars=100000,keep_recent=1,archive=store)
        memory.reasoning_origin='deepseek:api.deepseek.com';memory.reasoning_synthetic_empty=True
        # Replay exact final batch, all visible users, and one older archived step;
        # this intentionally isolates pressure from the saved last batch.
        user_seqs=[i+1 for i,m in enumerate(raw[:start]) if m['role']=='user']
        old_seq=next(i+1 for i,m in enumerate(raw[:start]) if m['role']=='assistant' and not m.get('tool_calls'))
        chosen=sorted(set(user_seqs+[old_seq]+list(range(start+1,end+1))))
        memory._history=[]
        for seq in chosen:
            item=copy.deepcopy(raw[seq-1]);item['_seq']=seq;memory._history.append(item)
        history=AgentHistory(store,'replay')
        history.begin_step(1);history.end_step()
        history.begin_step(2);history.end_step()
        with store.connect() as db:
            db.execute('UPDATE agent_context_steps SET first_seq=?,last_seq=? WHERE scope=? AND sequence=1',(old_seq,old_seq,store.scope))
            db.execute('UPDATE agent_context_steps SET first_seq=?,last_seq=? WHERE scope=? AND sequence=2',(start+1,end,store.scope))
        def model(purpose,messages):
            return {'content':json.dumps({'selections':[]} if purpose=='context_plan' else {'summary':'Prior step archived.'})}
        initial=counter.count_request(memory.for_model(),req['tools'])
        result=ContextConstructor(memory,history,model,model='deepseek-flash',token_counter=counter).rebuild('research',tools=req['tools'],input_budget_tokens=limit)
        print(group['strategy'],'before',initial,'preferred',result['preferred_target_tokens'],'protected_before',result['protected_tokens_before_reduction'],'protected_after',result['protected_tokens_after_reduction'],'after',result['local_input_tokens'],'budget',limit,'receipts',len(result['reduced_tool_results']),'protocol',memory.check())
        assert result['local_input_tokens']<=limit and not memory.check()
        for row in result['reduced_tool_results']:
            assert store.get(row['ref'])['content']

        if args.live:
            llm = agent.ChatLLM(key, os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com',
                                os.environ.get('AGENT_MODEL') or 'deepseek-flash')
            with contextlib.redirect_stdout(io.StringIO()):
                response = llm.chat(memory.for_model(), tools=req['tools'])
            print(group['strategy'], 'live_request_ok', True, 'tool_calls', len(response.get('tool_calls') or []),
                  'reported_usage', llm.usage_totals, 'unknown_calls', llm.usage_unknown)
