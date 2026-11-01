"""Astrion Research Eval v1: validate, prepare, run and independently review."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import agent
from evaluation.reporting import save_report
from .dataset import DEFAULT_BASE, DEFAULT_SOURCE, load_data, validate, prepare
from .runner import evaluate, summary
from .review import template, apply
from .model_judge import judge
from .config import DEFAULT_STEPS, DEFAULT_WORKERS, DEFAULT_WORKER_STEPS, MAX_WORKERS


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    val=sub.add_parser('validate',help='核对25篇原件哈希和题目证据锚点')
    val.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    prep=sub.add_parser('prepare',help='新建隔离考试资料库，不覆盖已有文件')
    prep.add_argument('--source',type=Path,default=DEFAULT_SOURCE);prep.add_argument('--base',type=Path,default=DEFAULT_BASE)
    listing=sub.add_parser('list',help='列出题目；不显示答案')
    listing.add_argument('--split',choices=['dev','holdout','all'],default='dev')
    run=sub.add_parser('run',help='运行真实模型；每题每组独立数据库和上下文')
    selection=run.add_mutually_exclusive_group(required=True)
    selection.add_argument('--case',action='append',help='指定题号，可重复')
    selection.add_argument('--split',choices=['dev','holdout','all'])
    run.add_argument('--mode',choices=['single','multi','both'],default='both')
    run.add_argument('--base',type=Path,default=DEFAULT_BASE)
    run.add_argument('--output',type=Path,default=Path('research_data/research_eval_v1/results'))
    run.add_argument('--steps',type=int,default=DEFAULT_STEPS,help='主 Agent 最大轮数（默认 10）')
    run.add_argument('--worker-steps',type=int,default=DEFAULT_WORKER_STEPS,help='每个子 Agent 最大轮数（默认 6）')
    run.add_argument('--workers',type=int,choices=range(MAX_WORKERS + 1),default=DEFAULT_WORKERS,help='子 Agent 总数，最多 3 个')
    run.add_argument('--budget-chars',type=int,default=12000)
    show=sub.add_parser('show',help='查看一道题的标准要点（不要提供给受测 Agent）');show.add_argument('case_id')
    rt=sub.add_parser('review-template',help='根据运行报告创建人工复核模板');rt.add_argument('report',type=Path);rt.add_argument('--output',type=Path,required=True)
    ar=sub.add_parser('apply-review',help='保存独立复核结果，不覆盖原始报告');ar.add_argument('report',type=Path);ar.add_argument('review',type=Path);ar.add_argument('--output',type=Path,default=Path('research_data/research_eval_v1/results/reviews'))
    mj=sub.add_parser('model-review',help='独立模型参考裁判；结果仍需人工复核')
    mj.add_argument('report',type=Path);mj.add_argument('--judge-model',required=True);mj.add_argument('--judge-base-url',default=None);mj.add_argument('--output',type=Path,default=Path('research_data/research_eval_v1/results/model-reviews'))
    su=sub.add_parser('summary',help='汇总已存在的报告');su.add_argument('reports',nargs='+',type=Path);su.add_argument('--output',type=Path,default=Path('research_data/research_eval_v1/results/summaries'))
    args=parser.parse_args(argv)
    try:
        _,cases=load_data()
        if args.command=='validate':
            result=validate(args.source)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['valid'] else 1
        if args.command=='prepare': result=prepare(args.source,args.base)
        elif args.command=='list':
            result=[{k:c[k] for k in ('id','split','type','task','paper_ids')} for c in cases if args.split=='all' or c['split']==args.split]
        elif args.command=='show':
            result=next((c for c in cases if c['id']==args.case_id),None)
            if result is None: raise ValueError('unknown_case_id')
        elif args.command=='review-template':
            data=json.loads(args.report.read_text());data['report_path']=str(args.report.resolve())
            result=template(data)
            with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
            result={'saved':str(args.output.resolve()),'instructions':'人工核对原文、填写每一条 reason 与状态后再 apply-review；不得把 unknown 当作通过。'}
        elif args.command=='apply-review': result=apply(args.report,args.review,args.output)
        elif args.command=='model-review':
            agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
            key=agent.first_nonempty(os.environ.get('ASTRION_JUDGE_API_KEY'),os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
            if not key:raise ValueError('未配置裁判模型 API key')
            url=args.judge_base_url or os.environ.get('ASTRION_JUDGE_BASE_URL') or os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com'
            result=judge(args.report,agent.ChatLLM(key,url,args.judge_model),args.output)
        elif args.command=='summary':
            reports=[]
            for path in args.reports:
                item=json.loads(path.read_text());item['report_path']=str(path.resolve());reports.append(item)
            result=summary(reports);result['report_path']=str(save_report(result,args.output,filename_prefix='research-suite'))
        else:
            selected=[c['id'] for c in cases if c['id'] in (args.case or [])] if args.case else [c['id'] for c in cases if args.split=='all' or c['split']==args.split]
            if args.case and (len(selected)!=len(set(args.case)) or not selected):raise ValueError('unknown_case_id')
            agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
            key=agent.first_nonempty(os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
            if not key:raise ValueError('未配置模型 API key')
            model=os.environ.get('AGENT_MODEL') or 'deepseek-chat';base_url=os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com'
            factory=lambda:agent.ChatLLM(key,base_url,model)
            modes=['single','multi'] if args.mode=='both' else [args.mode]
            print(f'将运行 {len(selected)} 道题 × {len(modes)} 组；真实模型请求会产生费用。',flush=True)
            reports=[];args.output.mkdir(parents=True,exist_ok=True)
            for idx,(case,mode) in enumerate(((c,m) for c in selected for m in modes),1):
                print(f'[{idx}/{len(selected)*len(modes)}] {case} {mode} 开始',flush=True)
                log=args.output / f'console-{case}-{mode}-{os.urandom(8).hex()}.log'
                with log.open('x',encoding='utf-8') as f,contextlib.redirect_stdout(f):
                    report=evaluate(case,mode,factory,args.output,base=args.base,steps=args.steps,worker_steps=args.worker_steps,workers=args.workers,budget_chars=args.budget_chars)
                reports.append(report)
                print(f"  状态：{report['case_outcome']}；语义复核：{report['semantic_assessment']}；报告：{report['report_path']}",flush=True)
            result=summary(reports);result['report_path']=str(save_report(result,args.output/'summaries',filename_prefix='research-suite'))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'错误：{type(exc).__name__}: {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
