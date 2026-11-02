"""Aggregate independent run manifests without inventing semantic pass rates."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def aggregate(paths, *, prefer_last=False):
    groups=[];seen={};superseded=[]
    for path in paths:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        for item in data['groups']:
            key=(item['case_id'],item['strategy'])
            row={**item,'manifest_path':str(Path(path).resolve())}
            if key in seen:
                if not prefer_last: raise ValueError('duplicate_case_strategy')
                index=seen[key]
                superseded.append(groups[index])
                groups[index]=row
            else:
                seen[key]=len(groups)
                groups.append(row)
    result={}
    for strategy in sorted({g['strategy'] for g in groups}):
        rows=[g for g in groups if g['strategy']==strategy]
        total=0;unknown=0
        for row in rows:
            usage=row.get('usage') or {}; value=(usage.get('totals') or {}).get('total_tokens')
            if value is not None:
                total+=value
            if (value is None or usage.get('unknown_calls') != 0 or row.get('status') in {'error','harness_error'}
                    or usage.get('transport_retry_tracking') is not True):
                unknown+=1
        result[strategy]={
            'runs':len(rows),'statuses':{k:sum(r['status']==k for r in rows) for k in sorted({r['status'] for r in rows})},
            'decision_attempts':sum((r.get('metrics') or {}).get('decision_calls',0) for r in rows),
            'both_updates_delivered':sum(bool((r.get('metrics') or {}).get('all_updates_delivered')) for r in rows),
            'long_process_runs':sum(bool((r.get('metrics') or {}).get('long_process_observed')) for r in rows),
            'ss_calls':sum((r.get('metrics') or {}).get('ss_calls',0) for r in rows),
            'planner_calls':sum((r.get('metrics') or {}).get('planner_calls',0) for r in rows),
            'known_total_tokens':total,'possibly_incomplete_usage_runs':unknown,
            'semantic_pass_rate':None,'semantic_status':'pending_independent_review',
        }
    attempts=[*groups,*superseded]
    all_known=sum(((row.get('usage') or {}).get('totals') or {}).get('total_tokens') or 0 for row in attempts)
    return {'kind':'long_research_comparison','groups':groups,'superseded_attempts':superseded,'strategies':result,
            'all_recorded_attempts_known_tokens':all_known,
            'note':'每题每种策略各一次；语义质量和标准要点草案仍须独立复核。'}


def markdown(result):
    lines=['# 长程科研测试汇总','',result['note'],'',
           '| 任务 | 策略 | 运行状态 | 决策请求 | 要求更新送达 | 可定位论文范围 | 引用位置有效/总数 | 已报告 token |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    for row in result['groups']:
        m=row.get('metrics') or {}; usage=(row.get('usage') or {}).get('totals') or {}
        lines.append(f"| {row['case_id']} | {row['strategy']} | {row['status']} | {m.get('decision_calls','?')} | {m.get('delivered_updates','?')}/{m.get('scheduled_updates','?')} | {len(m.get('papers_with_observed_ranges') or [])}/6 | {m.get('citations_in_observed_ranges','?')}/{m.get('citations_total','?')} | {usage.get('total_tokens','未知')} |")
    if result.get('superseded_attempts'):
        lines += ['',f"采用后续明确指定的重测记录；保留 {len(result['superseded_attempts'])} 条被替换的原尝试在 JSON 中。"]
    lines += ['','可定位范围只是成功返回且未标截断的片段，不等于读完整篇论文，也不等于引用支持结论。',
              '运行状态和引用位置不是任务正确率；原文、要点和回答仍需独立复核。',
              '观察到的单次结果不能作为统计显著性结论。','']
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifests',nargs='+',type=Path)
    p.add_argument('--output',required=True,type=Path,help='New JSON output, adjacent Markdown is also created')
    p.add_argument('--prefer-last',action='store_true',help='Explicitly select later manifests for duplicate attempts; preserve superseded entries')
    a=p.parse_args()
    if a.output.exists() or a.output.with_suffix('.md').exists():p.error('output exists')
    value=aggregate(a.manifests,prefer_last=a.prefer_last);a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
    with a.output.with_suffix('.md').open('x',encoding='utf-8') as f:f.write(markdown(value))
    print('汇总:',a.output.resolve());print('报告:',a.output.with_suffix('.md').resolve())

if __name__=='__main__':main()
