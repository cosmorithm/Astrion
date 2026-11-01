"""Compare matched QASPER runs without conflating failures and answer scores."""
from __future__ import annotations
import json
import re
import statistics
import uuid
from collections import Counter
from pathlib import Path
from . import qasper_analysis
from .qasper import sha256

VALID_STATUSES={'completed','no_tool_call'}


def root_steps(case):
    journals=list((Path(case['workspace'])/'runs').rglob('events.jsonl'))
    if len(journals)!=1:return None
    finished=[]
    try:
        with journals[0].open(encoding='utf-8') as f:
            for line in f:
                item=json.loads(line);event=item.get('event') or {}
                if item.get('role')=='root' and event.get('type')=='run.finished':
                    step=(event.get('payload') or {}).get('steps')
                    if type(step) is int and step>=0:finished.append(step)
    except (OSError,ValueError,TypeError):return None
    return finished[-1] if len(finished)==1 else None


def failure_kind(case):
    if case.get('status') != 'error': return None
    journals=list((Path(case['workspace'])/'runs').rglob('events.jsonl'))
    if len(journals)!=1:return 'unknown'
    try:
        with journals[0].open(encoding='utf-8') as f:
            for line in f:
                event=(json.loads(line).get('event') or {})
                if event.get('type')!='model.call.failed':continue
                message=str(((event.get('payload') or {}).get('error') or {}).get('message',''))
                if 'reasoning_content' in message and '400' in message:return 'deepseek_reasoning_protocol_400'
                if 'SSL' in message or 'URLError' in message or 'IncompleteRead' in message:return 'network_or_ssl'
                if '429' in message:return 'rate_limit_429'
                if 'timeout' in message.lower():return 'timeout'
                return 'model_request_error'
    except (OSError,ValueError,TypeError):pass
    return 'unknown'


def mean(values):
    return statistics.fmean(values) if values else None


def compare(paths,output,*,allow_retries=False):
    paths=[Path(p).resolve() for p in paths]
    if len(paths)<2:raise ValueError('at_least_two_groups_required')
    reports=[json.loads(p.read_text()) for p in paths]
    first=reports[0]
    if any(r.get('benchmark')!='QASPER' for r in reports):raise ValueError('not_qasper')
    def conditions(r):
        settings=dict(r['settings']);settings.pop('steps',None)
        return (r.get('version'),r.get('split'),r.get('dataset_sha256'),r.get('official_evaluator_sha256'),
                r.get('model'),settings)
    if any(conditions(r)!=conditions(first) for r in reports[1:]):raise ValueError('incompatible_conditions')
    steps=[r['settings']['steps'] for r in reports]
    if len(set(steps))!=len(steps):raise ValueError('duplicate_step_group')
    ids=[x['question_id'] for x in first['cases']]
    if len(set(ids))!=len(ids):raise ValueError('duplicate_case')
    if any(len(r['cases'])!=len(ids) or {c['question_id'] for c in r['cases']}!=set(ids) for r in reports):
        raise ValueError('case_sets_differ')
    if any(r.get('retry_suites') for r in reports) and not allow_retries:raise ValueError('retry_report_not_allowed')
    maps=[{c['question_id']:c for c in r['cases']} for r in reports]
    if any(any(m[q]['paper_id']!=maps[0][q]['paper_id'] for q in ids) for m in maps[1:]):
        raise ValueError('case_papers_differ')
    eligible={qid for qid in ids if all(m[qid].get('status') in VALID_STATUSES and
                                      (m[qid].get('predicted_answer') or '').strip() for m in maps)}
    target=Path(output)/('qasper-rounds-'+uuid.uuid4().hex);target.mkdir(parents=True,exist_ok=False)
    groups=[];diagnostics=[]
    for path,report,cases in zip(paths,reports,maps):
        analysis_path,analysis=qasper_analysis.analyze(path,target,k=5)
        diagnostics.append({c['question_id']:c for c in analysis['cases']})
        values=[root_steps(c) for c in report['cases']]
        successful=[c for c in report['cases'] if c.get('status') in VALID_STATUSES and (c.get('predicted_answer') or '').strip()]
        root_steps_known=[n for n in values if n is not None]
        versions=[]
        for suite in report['source_suites'] + report.get('retry_suites',[]):
            source=json.loads((Path(suite)/'summary.json').read_text())
            versions.append(source.get('code_version'))
        groups.append({'steps_limit':report['settings']['steps'],'sample_count':len(ids),
                       'case_statuses':dict(Counter(c.get('status','unknown') for c in report['cases'])),
                       'citation_statuses':dict(Counter((c.get('citation_audit') or {}).get('status','unknown') for c in report['cases'])),
                       'mean_answer_words':mean([len(re.findall(r'\w+',c.get('predicted_answer') or '')) for c in successful]),
                       'failure_categories':dict(Counter(failure_kind(c) for c in report['cases'] if c.get('status')=='error')),
                       'retry_count':len(report.get('retry_attempts',[])),
                       'retry_replaced':len(report.get('replaced_question_ids',[])),
                       'pre_retry_summary':report.get('original_summary'),
                       'successful_cases':len(successful),'paired_successful_cases':len(eligible),
                       'official_metrics_all':report['metrics'],
                       'successful_answer_f1':mean([(c.get('official_metrics') or {}).get('Answer F1') for c in successful
                                                    if (c.get('official_metrics') or {}).get('Answer F1') is not None]),
                       'successful_evidence_f1':mean([(c.get('official_metrics') or {}).get('Evidence F1') for c in successful
                                                      if (c.get('official_metrics') or {}).get('Evidence F1') is not None]),
                       'paired_answer_f1':mean([cases[q]['official_metrics']['Answer F1'] for q in eligible]),
                       'paired_evidence_f1':mean([cases[q]['official_metrics']['Evidence F1'] for q in eligible]),
                       'mean_root_steps':mean(root_steps_known),'root_steps_known':len(root_steps_known),
                       'at_limit':sum(n>=report['settings']['steps'] for n in root_steps_known),
                       'worker_count':sum(c.get('workers',0) for c in report['cases']),
                       'worker_runs':sum(c.get('workers',0)>0 for c in report['cases']),
                       'agent_duration_seconds_sum':report.get('duration_seconds'),
                       'mean_success_duration_seconds':mean([c['duration_seconds'] for c in successful if c.get('duration_seconds') is not None]),
                       'known_total_tokens':report.get('known_total_tokens'),
                       'unknown_token_runs':report.get('unknown_token_runs'),
                       'analysis':analysis['statistics'],'analysis_path':str(analysis_path/'summary.json'),
                       'source_summary':str(path),'source_sha256':sha256(path),'code_versions':versions})
    paired_retrieval={qid for qid in eligible if all(qid in d and d[qid]['retrieval']['status']=='measured' for d in diagnostics)}
    for group,diag in zip(groups,diagnostics):
        group['paired_retrieval_cases']=len(paired_retrieval)
        for key in ('first_search_recall_at_k','first_search_partial_recall_at_k','all_observed_recall'):
            group['paired_'+key]=mean([diag[q]['retrieval'][key] for q in paired_retrieval])
    rows=[]
    for qid in ids:
        rows.append({'question_id':qid,'paper_id':maps[0][qid]['paper_id'],
                     'paired_success':qid in eligible,
                     'groups':[{'steps_limit':report['settings']['steps'],'status':c.get('status'),'failure_kind':failure_kind(c),
                                'answer_f1':(c.get('official_metrics') or {}).get('Answer F1'),
                                'evidence_f1':(c.get('official_metrics') or {}).get('Evidence F1'),
                                'root_steps':root_steps(c),'workers':c.get('workers'),
                                'duration_seconds':c.get('duration_seconds'),
                                'total_tokens':(c.get('usage') or {}).get('totals',{}).get('total_tokens')}
                               for report,c in zip(reports,[m[qid] for m in maps])]})
    result={'kind':'qasper_rounds_comparison','model':first['model'],'split':first['split'],
            'dataset_sha256':first['dataset_sha256'],'settings_except_root_steps':conditions(first)[-1],
            'sample_count':len(ids),'paired_successful_cases':len(eligible),'paired_retrieval_cases':len(paired_retrieval),
            'includes_retries':any(g['retry_count'] for g in groups),'groups':groups,'cases':rows,
            'limitations':['single run per condition, non-random sample',
              'tool-result snippets and figure/table content can limit evidence metrics',
              'provider model/service and dirty worktree not fully pinned',
              'scores use model-output adaptation; F1 is not semantic correctness',
              'known token counts may omit failed calls']}
    (target/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (target/'REPORT.md').write_text(render_report(result),encoding='utf-8')
    return target,result



def render_report(result):
    def val(x):return '—' if x is None else f'{x:.3f}'
    def integer(x):return '—' if x is None else f'{x:,}'
    common=result['settings_except_root_steps']
    lines=['# QASPER 主 Agent 轮数对照', '',
           f"- {result['sample_count']} 道相同的 {result['split']} 题目；模型 `{result['model']}`。",
           f"- 每个子 Agent 最多 {common.get('worker_steps')} 轮，最多 {common.get('workers')} 个；软压缩阈值 {common.get('budget_chars')} 字符。",
           ('- 部分失败题经过重试；重试次数单独列出，与未重试组的结果不再是严格同重试策略对照。' if result.get('includes_retries') else
            '- 每组每题运行一次；题目非随机。'),
           '- 不同组可能同时运行，运行时长不应当视作严格的同负载对比。',
           '- 使用官方 QASPER 脚本，经 Astrion 的答案与实际已读引用映射；F1 衡量与参考答案/段落的重合，不是通过率或人工事实正确率。',
           '- 全样本评分对缺失预测按官方规则记零。配对评分只包含所有组都成功给出回答的相同题目。',
           '',f"## 得分与用量（配对题数：{result['paired_successful_cases']}）",'',
           '| 主轮数上限 | 成功题数 | 全样本 Answer F1 | 全样本 Evidence F1 | 配对 Answer F1 | 配对 Evidence F1 | 实际主轮数均值 | 到达上限题数 | 引用位置全通过 | 答案平均词数 | 重试次数 | 子 Agent 实际创建 | 已知 token | 用量未知题数 | 运行时长之和（秒） |',
           '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for group in result['groups']:
        metrics=group['official_metrics_all']
        lines.append(f"| {group['steps_limit']} | {group['successful_cases']}/{group['sample_count']} | {val(metrics.get('Answer F1'))} | {val(metrics.get('Evidence F1'))} | {val(group.get('paired_answer_f1'))} | {val(group.get('paired_evidence_f1'))} | {val(group.get('mean_root_steps'))} | {group['at_limit']} | {group['citation_statuses'].get('locations_found',0)} | {val(group.get('mean_answer_words'))} | {group['retry_count']} | {group['worker_count']} | {integer(group.get('known_total_tokens'))} | {integer(group.get('unknown_token_runs'))} | {val(group.get('agent_duration_seconds_sum'))} |")
    lines += ['', '未知 token 次数、逐题时长、代码版本及失败状态见 [机器可读汇总](summary.json)。实际主轮数只对有完整结束事件的运行计算；到达上限不一定代表任务未完成；引用位置有效不等于语义支持。答案词数由简单正则估算，不是模型 token。时长之和不是墙钟时间；token 不完整或并发运行的时长不可直接解释为同成本比较。',
              '',f"## 匹配题目的证据检索（题数：{result['paired_retrieval_cases']}）",'',
              '| 主轮数上限 | 首搜前 5 条完整证据召回 | 首搜前 5 条片段命中 | 搜索及阅读累计完整召回 |',
              '| ---: | ---: | ---: | ---: |']
    for group in result['groups']:
        lines.append(f"| {group['steps_limit']} | {val(group.get('paired_first_search_recall_at_k'))} | {val(group.get('paired_first_search_partial_recall_at_k'))} | {val(group.get('paired_all_observed_recall'))} |")
    lines += ['', '完整段落匹配与不少于 80 个连续字符的片段命中分开统计；标注分歧和无法完整映射的图表证据排除。累计召回不是 Recall@5。',
              '', '## 逐题结果', '', '| 论文 | 所有组均回答 | ' + ' | '.join(f"{g['steps_limit']} 轮 Answer / Evidence F1" for g in result['groups']) + ' |',
              '| --- | --- | ' + ' | '.join('---:' for _ in result['groups']) + ' |']
    for case in result['cases']:
        cells=[]
        for group in case['groups']:
            cells.append('失败或未回答' if group.get('status') not in VALID_STATUSES else f"{val(group.get('answer_f1'))} / {val(group.get('evidence_f1'))}")
        lines.append(f"| {case['paper_id']} | {'是' if case['paired_success'] else '否'} | " + ' | '.join(cells) + ' |')
    lines += ['', '说明：本次不是随机抽样或重复试验，模型服务与未提交工作树无法完整固定；即使数字不同，也不能仅凭本实验确定是轮数上限造成的。', '']
    return '\n'.join(lines)
