"""Create an independent human-review form; never used as an Agent prompt."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

from evaluation.research.dataset import load_data


def template(report_path):
    path=Path(report_path).resolve()
    raw=path.read_bytes(); report=json.loads(raw)
    if report.get('kind')=='long_research_fixed_history':
        fixed=report
        source=json.loads(Path(fixed['source_report']).read_text(encoding='utf-8'))
        if source.get('kind')!='long_research_eval' or source.get('case_id')!=fixed.get('source_case_id'):
            raise ValueError('fixed_source_report_mismatch')
        report={**source,'strategy':fixed['strategy']}
    elif report.get('kind')!='long_research_eval':
        raise ValueError('not_long_research_report')
    _,items=load_data(); by_id={c['id']:c for c in items}
    points=[]
    for case_id in report['source_case_ids']:
        for point in by_id[case_id]['key_points']:
            points.append({'id':point['id'],'expected_draft':point['expected'],
                           'source_evidence':point['evidence'],
                           'status':'pending','answer_quote':'','reason':''})
    return {'report_path':str(path),'report_sha256':hashlib.sha256(raw).hexdigest(),
            'case_id':report['case_id'],'strategy':report['strategy'],
            'gold_status':'provisional_needs_human_review',
            'reviewer':'','gold_reviewed':False,
            'task_completed':'pending','user_updates_followed':'pending',
            'citation_support':'pending','unsupported_claims':'pending',
            'notes':'','points':points}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    data=template(args.report)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as file:
        json.dump(data,file,ensure_ascii=False,indent=2)
    print('复核表:',args.output.resolve())

if __name__=='__main__':main()
