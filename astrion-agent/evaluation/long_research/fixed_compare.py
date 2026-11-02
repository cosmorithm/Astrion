"""Compare explicitly chosen fixed-history runs after verifying prefix identity."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def select(path,strategy):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    rows=[x for x in data['groups'] if x['strategy']==strategy]
    if len(rows)!=1:raise ValueError('expected_one_'+strategy)
    return {**rows[0],'manifest_path':str(Path(path).resolve())}


def compare(legacy_path,ss_path,*,superseded=()):
    old=select(legacy_path,'legacy'); new=select(ss_path,'ss')
    if not old.get('prefix_sha256') or old['prefix_sha256'] != new.get('prefix_sha256'):
        raise ValueError('fixed_prefix_mismatch')
    for key in ('source_prefix_steps','raw_local_tokens','input_budget_tokens'):
        if old.get(key)!=new.get(key):raise ValueError('fixed_setting_mismatch:'+key)
    # The prefix fingerprint includes the explicit continuation-step prompt.
    return {'kind':'long_research_fixed_comparison','groups':[old,new],
            'superseded_attempts':[str(Path(x).resolve()) for x in superseded],
            'semantic_pass_rate':None,'semantic_status':'pending_independent_review',
            'note':'两组前缀、工具及输入预算相同；每种策略独立续答一次，之前的失败尝试另行保留。'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--legacy',type=Path,required=True);p.add_argument('--ss',type=Path,required=True)
    p.add_argument('--superseded',type=Path,action='append',default=[])
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();md=a.output.with_suffix('.md')
    if a.output.exists() or md.exists():p.error('output exists')
    result=compare(a.legacy,a.ss,superseded=a.superseded)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    lines=['# 固定历史对照','',result['note'],'',f"共同前缀 SHA-256：`{result['groups'][0]['prefix_sha256']}`。",'',
           '| 策略 | 状态 | 决策请求 | SS 调用 | 压缩次数 | 已报告 token |',
           '|---|---|---:|---:|---:|---:|']
    for g in result['groups']:
        m=g.get('metrics') or {};t=(g.get('usage') or {}).get('totals') or {}
        lines.append(f"| {g['strategy']} | {g['result'].get('status')} | {m.get('decision_calls','?')} | {m.get('ss_calls','?')} | {m.get('compactions_applied','?')} | {t.get('total_tokens','未知')} |")
    lines += ['', '引用范围、事实正确性和完整报告均需独立复核；结果不是语义通过率。','']
    with md.open('x',encoding='utf-8') as f:f.write('\n'.join(lines))
    print('汇总:',a.output.resolve());print('报告:',md.resolve())

if __name__=='__main__':main()
