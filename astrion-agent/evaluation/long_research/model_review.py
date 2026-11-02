"""Provisional separate model review of a long-research report; not a human verdict."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from time import perf_counter

import agent
from context_tokenizer import load_token_counter
from evaluation.reporting import save_report
from evaluation.research import model_judge
from evaluation.research.runner import case_by_id


def build_inputs(report):
    parts=[]
    for source_id in report['source_case_ids']:
        proxy={**report,'case_id':source_id}
        parts.append(model_judge.inputs(proxy))
    points=[p for source_id in report['source_case_ids'] for p in case_by_id(source_id)['key_points']]
    if len({p['id'] for p in points}) != len(points):
        raise ValueError('duplicate_gold_point')
    pages={}
    for part in parts:
        for page in part['source_pages']:
            pages[(page['paper_id'],page['pdf_page'])]=page
    answer=parts[0]['answer']
    return {'question':report['run']['task'], 'key_points':points, 'answer':answer,
            'source_pages':list(pages.values()),'citations':parts[0]['citations'],
            'gold_status':'provisional_needs_human_review'}


def judge(report_path,llm,output):
    path=Path(report_path).resolve(); report=json.loads(path.read_text())
    if report.get('kind')=='long_research_fixed_history':
        fixed=report
        original=json.loads(Path(fixed['source_report']).read_text(encoding='utf-8'))
        if original.get('kind')!='long_research_eval' or original.get('case_id')!=fixed.get('source_case_id'):
            raise ValueError('fixed_source_report_mismatch')
        report={**original, 'strategy':fixed['strategy'], 'workspace':fixed['workspace'],
                'run':{**original['run'], 'result':fixed['result'], 'citation_audit':fixed['citation_audit']}}
    elif report.get('kind')!='long_research_eval':
        raise ValueError('not_long_research_report')
    data=build_inputs(report)
    messages=[{'role':'system','content':model_judge.SYSTEM},
              {'role':'user','content':json.dumps(data,ensure_ascii=False)}]
    tokens=load_token_counter(llm.model).count_request(messages,[])
    if tokens>100000:raise ValueError('judge_input_exceeds_100000_local_tokens')
    started=perf_counter()
    try:
        validation_errors=[]
        for attempt in range(2):
            request = messages if not attempt else [*messages, {
                'role':'user', 'content':'上次输出未通过格式校验：' + validation_errors[-1]
                + '。请重新输出完整 JSON；answer_quote 必须逐字复制回答中的连续短句，不能改写，无法找到时用空字符串并标记 unknown 或 incorrect。'}]
            if load_token_counter(llm.model).count_request(request,[]) > 100000:
                raise ValueError('judge_retry_input_exceeds_100000_local_tokens')
            response=llm.chat(request,tools=[])
            try:
                verdict=model_judge.validate_response(response.get('content'),{'key_points':data['key_points']},data['answer'])
                break
            except (ValueError,TypeError) as exc:
                validation_errors.append(str(exc)[:300])
        else:
            raise ValueError('judge_invalid_after_retry: ' + '; '.join(validation_errors))
        if not data['citations'] or any(not c['in_retrieved_range'] for c in data['citations']):
            verdict['citation_support']='fail'
            verdict['citation_reason']+='；位置检查发现引用缺失或未完整读到。'
        if any(c['truncated'] for c in data['citations']) and verdict['citation_support']=='pass':
            verdict['citation_support']='unknown'
            verdict['citation_reason']+='；部分引用过长或截断。'
        outcome=('fail' if any(p['status']=='incorrect' for p in verdict['points']) or verdict['citation_support']=='fail' or verdict['unsupported_claims']=='present'
                 else 'unknown' if any(p['status']=='unknown' for p in verdict['points']) or verdict['citation_support']=='unknown' or verdict['unsupported_claims']=='unknown'
                 else 'pass')
        if any(p['truncated'] for p in data['source_pages']) and outcome=='pass':outcome='unknown'
        diagnostics = {
            'correct':sum(p['status']=='correct' for p in verdict['points']),
            'incorrect_without_quote':sum(p['status']=='incorrect' and not p['answer_quote'] for p in verdict['points']),
            'incorrect_with_quote':sum(p['status']=='incorrect' and bool(p['answer_quote']) for p in verdict['points']),
            'unknown':sum(p['status']=='unknown' for p in verdict['points']),
        }
        result={'status':'completed','provisional_outcome':outcome,'verdict':verdict,
                'point_diagnostics':diagnostics,'validation_errors':validation_errors}
    except (Exception,SystemExit) as exc:
        result={'status':'error','provisional_outcome':'unknown','error':{'type':type(exc).__name__,'message':str(exc)[:500]}}
    result.update({'source_report':str(path),'judge_model':llm.model,'judge_input_local_tokens':tokens,
                   'same_model_as_agent':llm.model==report.get('model'),'source_pages_truncated':any(p['truncated'] for p in data['source_pages']),
                   'gold_status':'provisional_needs_human_review','duration_seconds':perf_counter()-started,
                   'usage':{'totals':llm.usage_totals,'calls':llm.usage_calls,'unknown_calls':llm.usage_unknown},
                   'note':'Separate invocation, not independent model weights; provisional only, human review required.'})
    saved=save_report(result,output,filename_prefix=report['case_id']+'-'+report['strategy']+'-judge')
    return {**result,'report_path':str(saved)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key=agent.first_nonempty(os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
    if not key:parser.error('missing model API key')
    llm=agent.ChatLLM(key,os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com',os.environ.get('AGENT_MODEL') or 'deepseek-flash')
    report=judge(args.report,llm,args.output)
    print('参考裁判:',report['status'],report['provisional_outcome'],'报告:',report['report_path'])

if __name__=='__main__':main()
