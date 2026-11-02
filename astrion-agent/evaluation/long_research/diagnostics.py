"""Compare observed source ranges with the final model request, without scoring truth."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re


def inspect(report):
    event_path=Path(report['run']['event_log'])
    events=[json.loads(line)['event'] for line in event_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    starts=[e for e in events if e['type']=='model.call.started' and e['payload'].get('purpose')=='decision']
    messages=((starts[-1]['payload'].get('request_snapshot') or {}).get('messages') or []) if starts else []
    historical=[]
    for message in messages:
        if message.get('role')=='tool':historical.append(message)
        if message.get('role')=='user':
            text=message.get('content') or ''
            marker=text.find('{"execution_history":')
            if marker>=0:
                try: historical.extend(json.loads(text[marker:]).get('execution_history',[]))
                except (ValueError,AttributeError):pass
    direct=set()
    for message in historical:
        if message.get('role')!='tool':continue
        try: content=json.loads(message.get('content') or '')
        except (TypeError,ValueError):continue
        if not isinstance(content,dict):continue
        for row in content.get('results') or []:
            if isinstance(row,dict) and type(row.get('document_id')) is int and not row.get('truncated'):
                direct.add(row['document_id'])
    mapping=report['paper_mapping']
    observed={r[0] for r in report['run'].get('observed_ranges') or []}
    answer=((report['run'].get('result') or {}).get('answer') or '')
    cited={int(x) for x in re.findall(r'\[D(\d+):L\d+',answer)}
    schedule=report['settings'].get('scheduled_user_updates') or {}
    updates=[int(k) for k,text in schedule.items() if any(m.get('role')=='user' and m.get('content')==text for m in messages)]
    return {'case_id':report['case_id'],'strategy':report['strategy'],
            'last_decision_step':starts[-1].get('step') if starts else None,
            'observed_papers':[p for p,d in mapping.items() if d in observed],
            'direct_untruncated_tool_papers_in_last_input':[p for p,d in mapping.items() if d in direct],
            'cited_papers':[p for p,d in mapping.items() if d in cited],
            'scheduled_updates_in_last_input':sorted(updates),
            'note':'Direct tool results only; summaries or previews may contain additional information. Presence is not semantic support.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('reports',nargs='+',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();data=[inspect(json.loads(path.read_text(encoding='utf-8'))) for path in a.reports]
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    print('诊断:',a.output.resolve())

if __name__=='__main__':main()
