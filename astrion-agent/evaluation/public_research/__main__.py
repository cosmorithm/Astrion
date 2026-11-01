"""Commands for Astrion integrations of published research benchmarks."""
import argparse
import json
import os
import subprocess
from pathlib import Path
import sys
import agent
from . import qasper, browsecomp, qasper_analysis, qasper_review, qasper_compare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('qasper-prepare')
    prepare.add_argument('--data', type=Path, default=Path('research_data/public_benchmarks/qasper'))
    run = sub.add_parser('qasper-run')
    run.add_argument('--data', type=Path, default=Path('research_data/public_benchmarks/qasper'))
    run.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    run.add_argument('--split', choices=('dev','test'), default='dev')
    run.add_argument('--count', type=int, default=2)
    run.add_argument('--offset', type=int, default=0, help='Skip this many papers in official order')
    run.add_argument('--steps', type=int, default=10)
    run.add_argument('--workers', type=int, choices=range(4), default=3)
    run.add_argument('--worker-steps', type=int, default=6)
    run.add_argument('--budget-chars', type=int, default=12000)
    merge = sub.add_parser('qasper-merge', help='Merge compatible, non-overlapping QASPER suites')
    merge.add_argument('suites', type=Path, nargs='+')
    merge.add_argument('--evaluator', type=Path, default=Path('research_data/public_benchmarks/qasper/qasper_evaluator.py'))
    merge.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    compare = sub.add_parser('qasper-compare', help='Compare matched QASPER runs with different main-agent limits')
    compare.add_argument('summaries', nargs='+', type=Path)
    compare.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    compare.add_argument('--allow-retries', action='store_true', help='Label and include separately reported retries')
    retry = sub.add_parser('qasper-apply-retries', help='Create a separate report replacing only failed cases')
    retry.add_argument('summary',type=Path)
    retry.add_argument('suites',nargs='+',type=Path)
    retry.add_argument('--evaluator',type=Path,default=Path('research_data/public_benchmarks/qasper/qasper_evaluator.py'))
    retry.add_argument('--output',type=Path,default=Path('research_data/public_benchmarks/results'))
    recover = sub.add_parser('qasper-recover', help='Recover finalized cases from an interrupted suite')
    recover.add_argument('partial', type=Path)
    recover.add_argument('--offset', type=int, required=True)
    recover.add_argument('--data', type=Path, default=Path('research_data/public_benchmarks/qasper'))
    recover.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    recover.add_argument('--split', choices=('dev','test'), default='dev')
    recover.add_argument('--steps', type=int, default=10)
    recover.add_argument('--workers', type=int, choices=range(4), default=3)
    recover.add_argument('--worker-steps', type=int, default=6)
    recover.add_argument('--budget-chars', type=int, default=12000)
    analysis = sub.add_parser('qasper-analyze', help='Analyze retrieval, citation and optional model evidence support')
    analysis.add_argument('summary', type=Path)
    analysis.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    analysis.add_argument('--k', type=int, default=5)
    analysis.add_argument('--model-review', action='store_true', help='Run provisional model reviewer; incurs cost')
    analysis.add_argument('--judge-model', help='Judge model name (defaults to AGENT_MODEL)')
    analysis.add_argument('--reuse', type=Path, help='Reuse matching prior reviews, preserving usage accounting')
    human = sub.add_parser('qasper-review-template', help='Prepare a local human support-review form')
    human.add_argument('summary', type=Path)
    human.add_argument('--output', type=Path, required=True)
    apply = sub.add_parser('qasper-apply-review', help='Validate a completed human review without overwriting history')
    apply.add_argument('summary', type=Path)
    apply.add_argument('review', type=Path)
    apply.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    download = sub.add_parser('browsecomp-download', help='Download all seven official fixed-corpus shards')
    download.add_argument('--corpus-dir', type=Path, default=Path('research_data/public_benchmarks/browsecomp_plus/corpus'))
    download.add_argument('--endpoint', default='https://huggingface.co')
    index = sub.add_parser('browsecomp-index', help='Index all seven official fixed-corpus parquet shards')
    index.add_argument('--corpus-dir', type=Path, default=Path('research_data/public_benchmarks/browsecomp_plus/corpus'))
    index.add_argument('--index', type=Path, default=Path('research_data/public_benchmarks/browsecomp_plus/corpus.sqlite3'))
    br = sub.add_parser('browsecomp-run', help='Run a small fixed-corpus diagnostic (official judge not included)')
    br.add_argument('--index', type=Path, default=Path('research_data/public_benchmarks/browsecomp_plus/corpus.sqlite3'))
    br.add_argument('--output', type=Path, default=Path('research_data/public_benchmarks/results'))
    br.add_argument('--query-parquet', required=True, help='Official first query parquet; local file or HTTPS URL')
    br.add_argument('--count', type=int, default=2)
    br.add_argument('--steps', type=int, default=10)
    br.add_argument('--budget-chars', type=int, default=12000)
    args = parser.parse_args(argv)
    try:
        if args.command == 'qasper-prepare':
            print(json.dumps(qasper.prepare(args.data), indent=2)); return 0
        if args.command == 'qasper-merge':
            path, result = qasper.merge(args.suites,args.evaluator,args.output)
            print(json.dumps({'summary_path':str(path/'summary.json'),'metrics':result['metrics']},indent=2)); return 0
        if args.command == 'qasper-compare':
            path,result=qasper_compare.compare(args.summaries,args.output,allow_retries=args.allow_retries)
            print(json.dumps({'summary_path':str(path/'summary.json'),'paired_successful_cases':result['paired_successful_cases']},ensure_ascii=False,indent=2));return 0
        if args.command == 'qasper-apply-retries':
            path,result=qasper.replace_failed(args.summary,args.suites,args.evaluator,args.output)
            print(json.dumps({'summary_path':str(path/'summary.json'),'replaced':len(result['replaced_question_ids']),
                              'metrics':result['metrics']},ensure_ascii=False,indent=2));return 0
        if args.command == 'qasper-recover':
            path,result=qasper.recover_partial(args.partial,args.data,args.output,offset=args.offset,split=args.split,
                steps=args.steps,workers=args.workers,worker_steps=args.worker_steps,budget_chars=args.budget_chars)
            print(json.dumps({'summary_path':str(path/'summary.json'),'metrics':result['metrics']},ensure_ascii=False,indent=2)); return 0
        if args.command == 'qasper-analyze':
            factory = None
            if args.model_review:
                agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
                key = agent.first_nonempty(os.getenv('ASTRION_JUDGE_API_KEY'),os.getenv('AGENT_API_KEY'),os.getenv('DEEPSEEK_API_KEY'),os.getenv('OPENAI_API_KEY'))
                if not key: raise ValueError('judge model API key not configured')
                url = os.getenv('ASTRION_JUDGE_BASE_URL') or os.getenv('AGENT_BASE_URL') or 'https://api.deepseek.com'
                model = args.judge_model or os.getenv('AGENT_MODEL') or 'deepseek-chat'
                factory = lambda:agent.ChatLLM(key,url,model)
            path,result = qasper_analysis.analyze(args.summary,args.output,k=args.k,judge_factory=factory,reuse=args.reuse)
            print(json.dumps({'summary_path':str(path/'summary.json'),'statistics':result['statistics']},ensure_ascii=False,indent=2)); return 0
        if args.command == 'qasper-review-template':
            result=qasper_review.template(args.summary)
            with args.output.open('x',encoding='utf-8') as f: json.dump(result,f,ensure_ascii=False,indent=2)
            print(json.dumps({'review_template':str(args.output.resolve()),'cases':len(result['cases'])},ensure_ascii=False)); return 0
        if args.command == 'qasper-apply-review':
            path,result=qasper_review.apply(args.summary,args.review,args.output)
            print(json.dumps({'summary_path':str(path/'summary.json'),'counts':result['counts']},ensure_ascii=False)); return 0
        if args.command == 'browsecomp-download':
            browsecomp.download_corpus(args.corpus_dir, args.endpoint); return 0
        if args.command == 'browsecomp-index':
            shards = sorted(args.corpus_dir.glob('train-*-of-00007.parquet'))
            print(json.dumps(browsecomp.build_index(shards, args.index), indent=2)); return 0
        agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
        key = agent.first_nonempty(os.getenv('AGENT_API_KEY'), os.getenv('DEEPSEEK_API_KEY'), os.getenv('OPENAI_API_KEY'))
        if not key: raise ValueError('model API key not configured')
        url = os.getenv('AGENT_BASE_URL') or 'https://api.deepseek.com'
        model = os.getenv('AGENT_MODEL') or 'deepseek-chat'
        print(f'Running {args.count} real model tasks; usage may incur costs.', flush=True)
        if args.command == 'browsecomp-run':
            queries = browsecomp.read_queries(args.query_parquet, args.count)
            path, result = browsecomp.run(args.index, queries, args.output, lambda:agent.ChatLLM(key,url,model),
                                          steps=args.steps, budget_chars=args.budget_chars)
            print(json.dumps({'summary_path':str(path/'summary.json'), 'scoring':result['scoring']},ensure_ascii=False,indent=2))
            return 0 if all(c['status'] != 'error' for c in result['cases']) else 1
        path, result = qasper.run(args.data, args.output, lambda:agent.ChatLLM(key,url,model), count=args.count,
            split=args.split, offset=args.offset, steps=args.steps, workers=args.workers, worker_steps=args.worker_steps, budget_chars=args.budget_chars)
        print(json.dumps({'summary_path':str(path/'summary.json'),'metrics':result['metrics']},ensure_ascii=False,indent=2))
        return 0 if all(c['status'] != 'error' for c in result['cases']) else 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
