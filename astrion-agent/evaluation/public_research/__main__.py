"""Commands for Astrion integrations of published research benchmarks."""
import argparse
import json
import os
import subprocess
from pathlib import Path
import sys
import agent
from . import qasper, browsecomp


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
