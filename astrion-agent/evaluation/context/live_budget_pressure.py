"""Real-model smoke test: one large batch must fit a local token budget.

Synthetic data only. Makes paid model requests; this is not a research-quality
or benchmark score. Use --output to choose a new JSON report.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

import agent
from context_store import ContextStore
from memory import Memory
from tooling import ToolSpec


QUESTION = ('请在第一轮并行调用 read_record，分别读取编号 1、2、3、4、5；'
            '然后依据编号 5 记录末尾的最终数字调用 finish，只返回该数字。')


def read_record(index):
    return ('资料编号 ' + str(index) + '；' + ('背景描述无关信息。' * 330)
            + '\n本记录的最终数字：' + ('450' if index == 5 else str(index)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists')
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'), os.environ.get('DEEPSEEK_API_KEY'),
                               os.environ.get('OPENAI_API_KEY'))
    if not key:
        parser.error('missing model API key')
    llm = agent.ChatLLM(key, os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com',
                        os.environ.get('AGENT_MODEL') or 'deepseek-flash')
    read_spec = ToolSpec('read_record', '读取编号 1 到 5 的一份长记录。', {
        'type': 'object', 'properties': {'index': {'type': 'integer'}},
        'required': ['index'], 'additionalProperties': False,
    })
    finish = next(s for s in agent.TOOL_SPECS if s.name == 'finish')
    trace = []
    with tempfile.TemporaryDirectory(prefix='astrion-budget-smoke-') as tmp:
        memory = Memory('你是严谨的资料分析助手。', budget_chars=100000, keep_recent=1,
                        archive=ContextStore(Path(tmp) / 'context.sqlite'))
        previous = os.environ.get('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS')
        os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS'] = '8500'
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                try:
                    result = agent.run(
                        QUESTION, llm, memory, max_steps=6,
                        tool_specs=[read_spec, finish],
                        tool_implementations={'read_record': read_record, 'finish': lambda answer: answer},
                        event_sink=trace.append, capture_model_input=True,
                    )
                except (Exception, SystemExit) as exc:
                    result = {'status': 'agent_failed', 'error': {
                        'type': type(exc).__name__, 'message': str(exc)[:500]}}
        finally:
            if previous is None:
                os.environ.pop('ASTRION_CONTEXT_INPUT_BUDGET_TOKENS', None)
            else:
                os.environ['ASTRION_CONTEXT_INPUT_BUDGET_TOKENS'] = previous
        root = Path(agent.__file__).resolve().parent
        files = ['agent.py', 'memory.py', 'context_construction.py', 'evaluation/context/live_budget_pressure.py']
        report = {
            'description': 'Synthetic data; actual model selects tool calls; local budget 8500.',
            'model': llm.model, 'result': result,
            'expected_answer': '450', 'correct': result.get('answer') == '450',
            'usage': {'known_totals': llm.usage_totals, 'reported_calls': llm.usage_calls,
                      'unknown_calls': llm.usage_unknown,
                      'observed_transport_retries': output.getvalue().count('[模型] 请求失败'),
                      'complete': (result.get('status') != 'agent_failed' and not llm.usage_unknown
                                   and '[模型] 请求失败' not in output.getvalue())},
            'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
            'trace': trace, 'archive_snapshot': memory.archive.snapshot(),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        print('status:', result['status'], 'correct:', report['correct'],
              'reported tokens:', llm.usage_totals['total_tokens'])
        print('report:', args.output.resolve())
        if result['status'] == 'agent_failed':
            raise SystemExit(1)


if __name__ == '__main__':
    main()
