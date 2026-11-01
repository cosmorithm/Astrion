"""Fixed acceptance tasks. All runs use a fresh project; optional live costs are explicit."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from time import perf_counter
import agent
from .store import Store
from .runner import run_research

FIXTURES=Path(__file__).resolve().parent.parent/'fixtures/research'


def stage1(store,project):
    for name in ['rag_en.md','agents_zh.md','reading_fixture.pdf']:store.add_document(project,FIXTURES/name)
    result=store.search(project,'如何利用检索到的文献生成有依据的回答',mode='semantic',limit=3)
    docs=store.documents(project)
    pdf=next(d['id'] for d in docs if d['name'].endswith('.pdf'))
    scan=store.document_store.read_part(project,pdf,page=3)
    return {'semantic':result['retrieval'],'semantic_results':[r['name'] for r in result['results']],
            'cross_language_retrieval':bool(result['results']) and result['results'][0]['name'] in {'rag_en.md','reading_fixture.pdf'},
            'scan_warning': 'missing_text' in scan['pages'][0]['warnings'],'document_count':len(docs)}


def stage2(store,project):
    from .providers.doi import LiteratureCatalog
    from .providers.web import WebReader
    from .providers.http import ServiceError
    checks={'search':None,'doi':None,'sources':[],'failures':[]}
    provider=LiteratureCatalog()
    try:
        found=provider.search('retrieval augmented generation',limit=3)
        checks['search']=found
        if found['results']:checks['doi']=provider.metadata(found['results'][0]['doi'],limit=3)
    except ServiceError as exc:checks['failures'].append({'operation':'literature','error':str(exc)})
    for url in ['https://arxiv.org/pdf/2005.11401','https://arxiv.org/abs/2312.10997']:
        try:
            result=WebReader(store).read(project,url)
            checks['sources'].append({'url':url,'document_id':result['document_id'],'snapshot':result['snapshot'],'kind':result['results'][0]['content_kind']})
        except ServiceError as exc:checks['failures'].append({'operation':'read','url':url,'error':str(exc)})
    return checks


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',type=int,choices=[1,2,3],required=True)
    p.add_argument('--db',type=Path,default=Path('research_data/acceptance.sqlite3'));p.add_argument('--live-model',action='store_true')
    p.add_argument('--steps',type=int,default=8);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();agent.load_env(agent.find_env_files(Path(agent.__file__).parent))
    store=Store(args.db);started=perf_counter()
    try:
        from uuid import uuid4
        project=store.create_project(f'acceptance-stage{args.stage}-{uuid4().hex[:8]}')
        cases=json.loads((FIXTURES/'cases.json').read_text())
        if args.stage==1:checks=stage1(store,project)
        elif args.stage==2:checks=stage2(store,project)
        else:raise ValueError('stage not installed')
        result={'stage':args.stage,'project_id':project,'checks':checks,'task':cases[f'stage{args.stage}']['task'],
                'evidence_support':{'status':'not_assessed','method':'manual claim-to-source review required'},'model_run':None}
        if args.live_model:
            key=agent.first_nonempty(os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
            if not key:raise ValueError('model_key_missing')
            llm=agent.ChatLLM(key,os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com',os.environ.get('AGENT_MODEL') or 'deepseek-chat')
            result['model_run']=run_research(store,project,result['task'],llm,max_steps=args.steps)
        result['duration_seconds']=perf_counter()-started
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'output':str(args.output),'checks':checks},ensure_ascii=False))
    finally:store.close()

if __name__=='__main__':main()
