"""Human semantic review with explicit unknowns and immutable source records."""
from __future__ import annotations
import json
import re
import sqlite3
import uuid
from pathlib import Path
from .qasper import sha256


STATUSES={'unknown','supported','partially_supported','unsupported'}


def template(analysis_path):
    analysis_path=Path(analysis_path).resolve();a=json.loads(analysis_path.read_text())
    if a.get('benchmark')!='QASPER diagnostic':raise ValueError('not_qasper_analysis')
    merged=json.loads(Path(a['source_summary']).read_text())
    if sha256(a['source_summary'])!=a['source_summary_sha256']:raise ValueError('source_summary_changed')
    gold={}
    for source in merged['source_suites']:
        for paper in json.loads((Path(source)/'gold.json').read_text()).values():
            for question in paper['qas']:
                if question['question_id'] in gold:raise ValueError('duplicate_question')
                gold[question['question_id']]=question
    raw={c['question_id']:c for c in merged['cases']}
    if len(raw)!=len(merged['cases']) or set(raw)!=set(gold):raise ValueError('source_cases_mismatch')
    reviews=[]
    for item in a['cases']:
        qid=item['question_id'];case=raw[qid];question=gold[qid]
        citations=[];db=Path(case['workspace'])/'research.sqlite3'
        con=sqlite3.connect(f'file:{db.resolve()}?mode=ro',uri=True) if db.is_file() else None
        try:
            for check in (case.get('citation_audit') or {}).get('checks',[]):
                evidence={**check,'text':None,'truncated':False}
                match=re.fullmatch(r'\[D(\d+):L(\d+)-L(\d+)\]',check.get('citation',''))
                if con is not None and match:
                    doc,start,end=map(int,match.groups())
                    row=con.execute('SELECT content FROM documents WHERE id=?',(doc,)).fetchone()
                    if row and 1<=start<=end<=len(row[0].splitlines()):
                        text='\n'.join(row[0].splitlines()[start-1:end]);evidence['text']=text[:12000];evidence['truncated']=len(text)>12000
                citations.append(evidence)
        finally:
            if con:con.close()
        refs=[]
        for ref in question.get('answers',[]):
            answer=ref['answer'];refs.append({k:answer.get(k) for k in ('unanswerable','extractive_spans','free_form_answer','yes_no','evidence')})
        reviews.append({'question_id':qid,'paper_id':item['paper_id'],'question':question['question'],
                        'answer':case.get('answer'),'citations':citations,'reference_annotations':refs,
                        'model_review':item.get('support_review'), 'verdict':'unknown','reason':''})
    if len(reviews)!=len(raw) or len({x['question_id'] for x in reviews})!=len(raw):raise ValueError('analysis_cases_mismatch')
    return {'kind':'qasper_human_review_template','analysis_path':str(analysis_path),'analysis_sha256':sha256(analysis_path),
            'reviewer':'','instructions':'Reference annotations and model reviews are fallible. Review source and answer; unknown remains unscored.',
            'cases':reviews}


def apply(analysis_path,review_path,output):
    analysis_path=Path(analysis_path).resolve();a=json.loads(analysis_path.read_text())
    review=json.loads(Path(review_path).read_text())
    if a.get('benchmark')!='QASPER diagnostic' or review.get('kind')!='qasper_human_review_template':raise ValueError('invalid_review')
    if review.get('analysis_sha256')!=sha256(analysis_path):raise ValueError('analysis_changed')
    if not isinstance(review.get('reviewer'),str) or not review['reviewer'].strip():raise ValueError('reviewer_required')
    expected={c['question_id'] for c in a['cases']};rows=review.get('cases')
    if not isinstance(rows,list) or len(rows)!=len(expected) or {x.get('question_id') for x in rows}!=expected:raise ValueError('review_cases_mismatch')
    canonical={x['question_id']:x for x in template(analysis_path)['cases']}
    immutable=('paper_id','question','answer','citations','reference_annotations','model_review')
    counts={key:0 for key in STATUSES};records=[]
    for row in rows:
        if any(row.get(key)!=canonical[row['question_id']].get(key) for key in immutable):
            raise ValueError('review_source_fields_changed')
        status=row.get('verdict');reason=row.get('reason')
        if status not in STATUSES or not isinstance(reason,str) or (status!='unknown' and not reason.strip()):raise ValueError('invalid_verdict_or_reason')
        counts[status]+=1;records.append({'question_id':row['question_id'],'verdict':status,'reason':reason})
    decided=len(rows)-counts['unknown']
    result={'kind':'qasper_human_review','analysis_path':str(analysis_path),'analysis_sha256':sha256(analysis_path),
            'reviewer':review['reviewer'].strip(),'counts':counts,'decided':decided,
            'supported_rate_among_decided':counts['supported']/decided if decided else None,
            'all_case_supported_rate':counts['supported']/len(rows) if rows and not counts['unknown'] else None,
            'cases':records}
    target=Path(output)/('qasper-human-review-'+uuid.uuid4().hex);target.mkdir(parents=True,exist_ok=False)
    (target/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return target,result
