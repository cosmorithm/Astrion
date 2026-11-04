"""Validate the curated evidence against local PDF and extracted snapshots."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent


def validate(source, base):
    data = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f'file:{Path(base).resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    errors = []
    try:
        for pid, paper in data['papers'].items():
            path = Path(source) / paper['name']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != paper['sha256']:
                errors.append(f'{pid}: pdf_missing_or_version_mismatch')
            row = db.execute('SELECT * FROM documents WHERE id=?', (paper['document_id'],)).fetchone()
            if row is None:
                errors.append(f'{pid}: document_missing')
                continue
            meta = json.loads(row['metadata'])
            if (row['sha256'] != paper['sha256'] or row['name'] != paper['name']
                    or not row['original'] or hashlib.sha256(row['original']).hexdigest() != paper['sha256']
                    or meta.get('text_sha256') != paper['text_sha256']
                    or hashlib.sha256(row['content'].encode()).hexdigest() != paper['text_sha256']):
                errors.append(f'{pid}: extracted_version_mismatch')
        case_ids = [case['id'] for case in data['cases']]
        check_ids = [check['id'] for case in data['cases'] for check in case['checks']]
        if len(case_ids) != len(set(case_ids)):
            errors.append('duplicate_case_id')
        if len(check_ids) != len(set(check_ids)):
            errors.append('duplicate_check_id')
        for case in data['cases']:
            for check in case['checks']:
                for evidence in check['evidence']:
                    paper = data['papers'].get(evidence['paper_id'])
                    if paper is None:
                        errors.append(f"{check['id']}: unknown_paper")
                        continue
                    row = db.execute('SELECT content, metadata FROM documents WHERE id=?',
                                     (paper['document_id'],)).fetchone()
                    if row is None:
                        continue
                    pages = json.loads(row['metadata']).get('pages', [])
                    page = next((p for p in pages if p['page'] == evidence['pdf_page']), None)
                    start, end = evidence['start_line'], evidence['end_line']
                    if (type(start) is not int or type(end) is not int or not page
                            or not 1 <= page['start_line'] <= start <= end <= page['end_line']
                            or end > len(row['content'].splitlines())
                            or not '\n'.join(row['content'].splitlines()[start - 1:end]).strip()):
                        errors.append(f"{check['id']}: invalid_page_or_lines")
    finally:
        db.close()
    return {'valid': not errors, 'cases': len(data['cases']),
            'checks': sum(len(case['checks']) for case in data['cases']),
            'papers': len(data['papers']), 'status': data['status'], 'errors': errors,
            'note': '检查版本和位置，不替代独立人工对原文与语义的复核。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'run-case', 'run-update'])
    parser.add_argument('--source', default='/Users/apple/Desktop/科研/Agent Test Paper')
    parser.add_argument('--base', default='research_data/research_eval_v1/corpus.sqlite3')
    parser.add_argument('--output', default='research_data/research_eval_v1/acceptance_v2')
    parser.add_argument('--steps', type=int, default=10)
    parser.add_argument('--case', help='题号；R21 使用单 Agent 及第 3 轮注入需求')
    parser.add_argument('--mode', choices=['single', 'multi'], default='single')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--worker-steps', type=int, default=6)
    parser.add_argument('--toolset', choices=['baseline', 'claims'], default='claims')
    parser.add_argument('--budget-chars', type=int, default=12000)
    args = parser.parse_args()
    if args.command == 'validate':
        result = validate(args.source, args.base)
    else:
        import os
        import agent
        from .run_update import run_update
        agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
        key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'), os.environ.get('DEEPSEEK_API_KEY'), os.environ.get('OPENAI_API_KEY'))
        if not key:
            raise ValueError('未配置模型 API key')
        url = os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com'
        model = os.environ.get('AGENT_MODEL') or 'deepseek-chat'
        factory = lambda: agent.ChatLLM(key, url, model)
        case_id = 'R21' if args.command == 'run-update' else args.case
        if not case_id:
            parser.error('run-case requires --case')
        if case_id == 'R21':
            if args.mode != 'single':
                parser.error('R21 currently supports only single mode')
            result = run_update(factory, args.output, source=args.source, base=args.base,
                                steps=args.steps, budget_chars=args.budget_chars, toolset=args.toolset)
        else:
            from .run_case import run_case
            result = run_case(case_id, factory, args.output, source=args.source, base=args.base,
                              mode=args.mode, steps=args.steps, workers=args.workers,
                              worker_steps=args.worker_steps, budget_chars=args.budget_chars,
                              toolset=args.toolset)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == 'validate' and not result['valid']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
