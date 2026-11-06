"""Run an existing question with the selected toolset, retaining its original report."""
import hashlib
import json
from pathlib import Path

from evaluation.reporting import save_report
from evaluation.research.runner import ALLOWED, case_by_id, evaluate
from .__main__ import HERE, validate


def run_case(case_id, llm_factory, output, *, source, base, mode='single', steps=10,
             workers=3, worker_steps=6, budget_chars=12000, toolset='claims',
             delegation_mode='baseline', include_worker_reader=False):
    if toolset not in {'baseline', 'claims'}:
        raise ValueError('unknown_toolset')
    if not validate(source, base)['valid']:
        raise ValueError('acceptance_source_validation_failed')
    data_path = HERE / 'cases.json'
    data = json.loads(data_path.read_text(encoding='utf-8'))
    case = next((item for item in data['cases'] if item['id'] == case_id), None)
    if not case or case_id == 'R21':
        raise ValueError('unknown_or_special_case_id')
    legacy_case = case_by_id(case_id)
    if any(case[key] != legacy_case[key] for key in ('task', 'paper_ids')):
        raise ValueError('acceptance_case_differs_from_v1')
    allowed = ALLOWED | ({'record_claim', 'list_claims'} if toolset == 'claims' else set())
    if include_worker_reader:
        allowed.add('read_worker_report')
    report = evaluate(case_id, mode, llm_factory, output, base=Path(base), steps=steps,
                      workers=workers, worker_steps=worker_steps, budget_chars=budget_chars,
                      tool_allowlist=allowed, delegation_mode=delegation_mode)
    original_path = report.pop('report_path')
    report['acceptance_v2'] = {
        'cases_sha256': hashlib.sha256(data_path.read_bytes()).hexdigest(),
        'check_ids': [item['id'] for item in case['checks']],
        'source_and_gold_review': 'assistant_reviewed_human_pending',
        'semantic_review': 'pending', 'toolset': toolset, 'delegation_mode': delegation_mode,
        'include_worker_reader': include_worker_reader,
        'added_to_v1_allowlist': sorted(allowed - ALLOWED),
        'original_run_report': original_path,
        'note': '请按 v2 检查点独立核对原文、答案与支持关系；旧题库复核模板不等同于 v2 检查点。',
    }
    report['report_path'] = str(save_report(report, Path(output) / 'acceptance-reports',
                                           filename_prefix=f'{case_id}-{toolset}'))
    return report
