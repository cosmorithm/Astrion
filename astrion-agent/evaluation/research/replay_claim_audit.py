"""Replay claim checks on a copied archive; never overwrite the source report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from time import perf_counter

from memory import Memory
from research.claims import assess_claims
from research.claim_review import review_claims


def replay(report_path, output_path, llm=None):
    source = Path(report_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source == output:
        raise ValueError('refuse_to_overwrite_source_report')
    if output.exists():
        raise FileExistsError(f'回放输出已存在：{output}')
    raw = source.read_bytes()
    data = json.loads(raw)
    run = data.get('run', data)
    archive = run['context_archive']
    archive_path = Path(archive['path']).expanduser().resolve()
    if output == archive_path:
        raise ValueError('refuse_to_overwrite_source_archive')
    answer = run['result']['answer']
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix='astrion-claim-replay-') as temporary:
        snapshot = Path(temporary) / 'context.sqlite3'
        src = sqlite3.connect(archive_path.as_uri() + '?mode=ro', uri=True)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        copied = Memory.from_archive(snapshot, archive['scope'])
        memory = Memory.from_dict(copied.to_dict())
        audit = review_claims(memory, answer, llm) if llm is not None else assess_claims(memory, answer)
    result = {'kind': 'claim_audit_replay', 'source_report': str(source),
              'source_report_sha256': hashlib.sha256(raw).hexdigest(),
              'archive_snapshot_sha256': snapshot_sha, 'scope': archive['scope'],
              'source_run_id': run.get('run_id'), 'model_review': llm is not None,
              'duration_seconds': perf_counter() - started, 'audit': audit,
              'note': '独立回放未修改历史；引用定位不等于语义支持，未登记结论覆盖未知。'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--output', required=True, help='新的结果路径，已有文件不会覆盖')
    parser.add_argument('--model-review', action='store_true', help='调用模型复核，产生费用')
    args = parser.parse_args()
    llm = None
    if args.model_review:
        import os
        import agent
        agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
        key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'), os.environ.get('DEEPSEEK_API_KEY'), os.environ.get('OPENAI_API_KEY'))
        if not key:
            raise ValueError('未配置模型 API key')
        llm = agent.ChatLLM(key, os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com',
                            os.environ.get('AGENT_MODEL') or 'deepseek-chat')
    result = replay(args.report, args.output, llm)
    print(json.dumps({'output': str(Path(args.output).resolve()),
                      'claims': len(result['audit']['claims']),
                      'location_valid': sum(x['location_valid'] for x in result['audit']['claims']),
                      'review': result['audit'].get('review', {}).get('status', 'not_requested')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
