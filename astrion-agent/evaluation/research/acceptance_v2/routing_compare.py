"""Paired baseline/router exploratory runs; no semantic pass is inferred."""
from pathlib import Path
from evaluation.reporting import save_report
from .run_case import run_case


def _observation(report):
    run = report.get('run') or {}
    result = run.get('result') if isinstance(run.get('result'), dict) else {}
    usage = run.get('usage_including_root_review')
    totals = usage.get('totals') if isinstance(usage, dict) else None
    known = totals.get('total_tokens') if isinstance(totals, dict) else None
    unknown = usage.get('unknown_calls') if isinstance(usage, dict) else None
    stages = run.get('continuation_stages')
    stages = [s for s in stages if isinstance(s, dict)] if isinstance(stages, list) else []
    workers = ([w for stage in stages for w in (stage.get('workers') if isinstance(stage.get('workers'), list) else [])]
               if stages else (run.get('workers') if isinstance(run.get('workers'), list) else []))
    routing = ([stage.get('delegation_routing') for stage in stages] if stages else run.get('delegation_routing'))
    return {
        'report_path': report.get('report_path'), 'run_id': run.get('run_id'),
        'status': result.get('status', run.get('status', 'unknown')),
        'answer_present': bool(result.get('answer')),
        'worker_count': len(workers), 'worker_statuses': [w.get('status') for w in workers if isinstance(w, dict)],
        'delegation_routing': routing,
        'citation_location_status': (run.get('citation_audit') or {}).get('status', 'unknown'),
        'semantic_assessment': report.get('semantic_assessment', 'pending'),
        'duration_seconds': run.get('duration_seconds'), 'usage_including_root_review': usage,
        'total_tokens_known': known, 'token_completeness': 'complete' if type(unknown) is int and unknown == 0 and known is not None else 'unknown_or_partial',
        'settings': report.get('settings'), 'code_version': report.get('code_version'),
    }


def compare_routing(case_id, llm_factory, output, *, source, base, steps=10, workers=3,
                    worker_steps=6, budget_chars=12000, toolset='claims'):
    if type(workers) is not int or workers < 1:
        raise ValueError('comparison_requires_workers')
    observations = {}
    for routing in ('baseline', 'structured'):
        try:
            report = run_case(case_id, llm_factory, output, source=source, base=base, mode='multi',
                              steps=steps, workers=workers, worker_steps=worker_steps,
                              budget_chars=budget_chars, toolset=toolset, delegation_mode=routing,
                              include_worker_reader=True)
            observations[routing] = _observation(report)
        except (Exception, SystemExit) as exc:
            observations[routing] = {'status': 'experiment_error', 'error_type': type(exc).__name__,
                                     'semantic_assessment': 'pending'}
    baseline, structured = observations['baseline'], observations['structured']
    same_tools = None
    if isinstance(baseline.get('settings'), dict) and isinstance(structured.get('settings'), dict):
        a, b = baseline['settings'].get('allowed_tools'), structured['settings'].get('allowed_tools')
        if isinstance(a, list) and isinstance(b, list):
            same_tools = a == b
    result = {
        'kind': 'research_routing_pair', 'case_id': case_id, 'modes': observations,
        'same_allowed_tools': same_tools, 'semantic_assessment': 'pending',
        'note': ('探索性单题配对；两组均显式开放同一 read_worker_report 白名单。'
                 '委派次数不是质量分数；引用位置不等于结论受原文支持，答案需独立人工复核。'
                 '不同总模型调用与 token 成本须分别报告，单次结果不能证明性能提升。'),
    }
    result['report_path'] = str(save_report(result, Path(output) / 'routing-comparisons', filename_prefix=f'{case_id}-routing'))
    return result
