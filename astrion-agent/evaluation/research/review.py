"""Explicit human review; pending and unknown never become passes."""
import json
from datetime import datetime, timezone
from pathlib import Path
from evaluation.reporting import save_report
from .runner import case_by_id


def template(report):
    case = case_by_id(report['case_id'])
    return {'report_path': report['report_path'], 'reviewer': '', 'gold_reviewed': False,
            'citation_support': 'unknown', 'citation_reason': '',
            'unsupported_claims': 'unknown', 'unsupported_claims_reason': '',
            'points': [{'id':p['id'], 'status':'unknown', 'reason':''} for p in case['key_points']]}


def apply(report_path, review_path, output):
    report_path=Path(report_path).resolve(); review_path=Path(review_path).resolve()
    report=json.loads(report_path.read_text()); review=json.loads(review_path.read_text())
    if report.get('kind')!='research_eval_case': raise ValueError('invalid_case_report')
    if Path(review.get('report_path','')).resolve()!=report_path: raise ValueError('review_report_mismatch')
    if not isinstance(review.get('reviewer'),str) or not review['reviewer'].strip(): raise ValueError('reviewer_required')
    if type(review.get('gold_reviewed')) is not bool: raise ValueError('gold_reviewed_must_be_boolean')
    if review.get('citation_support') not in {'pass','fail','unknown'}: raise ValueError('invalid_citation_support')
    if not isinstance(review.get('citation_reason'),str) or not review['citation_reason'].strip(): raise ValueError('citation_reason_required')
    if review.get('unsupported_claims') not in {'none','present','unknown'}: raise ValueError('invalid_unsupported_claims')
    if not isinstance(review.get('unsupported_claims_reason'),str) or not review['unsupported_claims_reason'].strip(): raise ValueError('unsupported_claims_reason_required')
    expected={p['id'] for p in case_by_id(report['case_id'])['key_points']}
    entries=review.get('points')
    if not isinstance(entries,list) or len(entries)!=len(expected): raise ValueError('review_points_mismatch')
    if {p.get('id') for p in entries if isinstance(p,dict)}!=expected: raise ValueError('review_points_mismatch')
    for point in entries:
        if point.get('status') not in {'correct','incorrect','unknown'} or not isinstance(point.get('reason'),str) or not point['reason'].strip():
            raise ValueError('each_point_needs_status_and_reason')
    reviewed={**report,'human_review':review,'reviewed_at':datetime.now(timezone.utc).isoformat(),
              'original_report_path':str(report_path)}
    statuses=[p['status'] for p in entries]
    if any(j['status']=='fail' for j in report['judgments']):
        outcome='fail'
    elif not review['gold_reviewed'] or 'unknown' in statuses or review['citation_support']=='unknown' or review['unsupported_claims']=='unknown':
        outcome='pending'
    elif 'incorrect' in statuses or review['citation_support']=='fail' or review['unsupported_claims']=='present':
        outcome='fail'
    else:
        outcome='pass'
    reviewed['case_outcome']=outcome
    reviewed['semantic_assessment']=('human_reviewed' if review['gold_reviewed'] and 'unknown' not in statuses
                                     and review['citation_support']!='unknown' and review['unsupported_claims']!='unknown'
                                     else 'pending')
    path=save_report(reviewed, output, filename_prefix=report['case_id']+'-'+report['mode']+'-reviewed')
    return {**reviewed,'report_path':str(path)}
