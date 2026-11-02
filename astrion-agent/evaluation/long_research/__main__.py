"""Run the long research comparison; writes independent reports after each run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import agent
from evaluation.research.dataset import DEFAULT_BASE
from .runner import cases, run_case


def markdown(manifest):
    lines = ["# Astrion 长程科研对照", "", f"模型：`{manifest['model']}`。",
             "题目为指定论文的封闭资料任务；真实轨迹和工作区按组隔离。",
             "位置检查只确认引用范围已读，不判断论断是否被支持；语义和金标准均待独立人工复核。",
             "若未发生压缩或未达到长过程，仅说明本次没有覆盖对应压力，不能视为方案失败。", "",
             "| 任务 | 方案 | 状态 | 决策轮数 | 更新送达 | 实际压缩 | 已观察论文 | 引用位置有效/总数 | 已报告 token |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for group in manifest['groups']:
        m = group.get('metrics') or {}; totals = (group.get('usage') or {}).get('totals') or {}
        updates = f"{m.get('delivered_updates', '?')}/{m.get('scheduled_updates', '?')}"
        citations = f"{m.get('citations_in_observed_ranges', '?')}/{m.get('citations_total', '?')}"
        seen = len(m.get('papers_with_observed_ranges') or [])
        lines.append(f"| {group['case_id']} | {group['strategy']} | {group['status']} | {m.get('decision_calls','?')} | {updates} | {m.get('compactions_applied','?')} | {seen}/6 | {citations} | {totals.get('total_tokens','未知')} |")
    lines += ["", "以上不是语义正确率或任务通过率；请结合每组原始报告、论文和复核表逐项核查。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--strategy', choices=('both','legacy','ss'), default='both')
    parser.add_argument('--steps', type=int, default=30)
    parser.add_argument('--budget-chars', type=int, default=16000)
    parser.add_argument('--input-budget-tokens', type=int, default=12000)
    parser.add_argument('--base', type=Path, default=DEFAULT_BASE)
    parser.add_argument('--output', type=Path, required=True, help='New suite directory; never overwritten')
    args = parser.parse_args(argv)
    items = cases()
    lookup = {c['id']:c for c in items}
    if args.case:
        if len(set(args.case)) != len(args.case) or any(k not in lookup for k in args.case):
            parser.error('unknown or duplicate case')
        items = [lookup[k] for k in args.case]
    if not 6 <= args.steps <= 50 or args.budget_chars < 1 or args.input_budget_tokens < 1:
        parser.error('steps must be 6..50, budgets must be positive')
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(os.environ.get('AGENT_API_KEY'),os.environ.get('DEEPSEEK_API_KEY'),os.environ.get('OPENAI_API_KEY'))
    if not key: parser.error('missing model API key')
    base_url = os.environ.get('AGENT_BASE_URL') or 'https://api.deepseek.com'
    model = os.environ.get('AGENT_MODEL') or 'deepseek-flash'
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'schema':'astrion-long-research-suite-v1','suite_id':'long_'+uuid4().hex,
                'model':model,'groups':[], 'note':'Semantic support and task quality require independent human review.'}
    manifest_path = output/'suite.json'
    def write():
        temp = output/'suite.tmp'
        temp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(manifest_path)
        (output/'suite.md').write_text(markdown(manifest),encoding='utf-8')
    write()
    for index, case in enumerate(items):
        order = (('legacy','ss') if int(case['id'][-2:]) % 2 else ('ss','legacy')) if args.strategy=='both' else (args.strategy,)
        for strategy in order:
            print('开始',case['id'],strategy,flush=True,file=sys.stderr)
            try:
                report = run_case(case,strategy,lambda:agent.ChatLLM(key,base_url,model),output,
                    base=args.base,max_steps=args.steps,budget_chars=args.budget_chars,
                    input_budget_tokens=args.input_budget_tokens)
                item = {'case_id':case['id'],'strategy':strategy,'report_path':report['report_path'],
                        'status':(report['run'].get('result') or {}).get('status'),
                        'metrics':report['metrics'],'usage':report['run'].get('usage'),
                        'duration_seconds':report['duration_seconds'],'error':report['error']}
            except (Exception,SystemExit) as exc:
                item = {'case_id':case['id'],'strategy':strategy,'status':'harness_error',
                        'error':{'type':type(exc).__name__,'message':str(exc)[:1000]}}
            manifest['groups'].append(item);write()
            print('完成',case['id'],strategy,item['status'],flush=True,file=sys.stderr)
    print('总报告:',manifest_path)
    return 0 if all(g['status'] != 'harness_error' for g in manifest['groups']) else 1

if __name__ == '__main__':
    raise SystemExit(main())
