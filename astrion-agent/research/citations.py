"""引用位置检查；不等价于语义支持评判。"""
import json
import re

def covers_range(observed, document_id, start, end):
    """多个相邻/重叠片段可覆盖一个引用，但不可跨过未读行。"""
    if start > end:
        return False
    cursor = start
    for doc, seen_start, seen_end in sorted(observed):
        if doc != document_id or seen_end < cursor:
            continue
        if seen_start > cursor:
            return False
        cursor = max(cursor, seen_end + 1)
        if cursor > end:
            return True
    return False


def audit_citations(answer, trace):
    """赛后核对引用位置是否在本次实际检索结果中，不判断语义是否支持结论。"""
    citations = re.findall(r'\[D(\d+):L(\d+)-L(\d+)\]', answer or '')
    observed = []
    for item in trace:
        event = item['event']
        payload = event.get('payload', {})
        if event['type'] != 'tool.call.finished' or payload.get('name') not in {'search_knowledge', 'read_knowledge', 'read_document', 'read_webpage'} or payload.get('outcome') != 'success':
            continue
        for found in json.loads(payload['content']).get('results', []):
            if not found.get('truncated'):
                observed.append((found['document_id'], found['start_line'], found['end_line']))
    checks = []
    for doc, start, end in citations:
        doc, start, end = int(doc), int(start), int(end)
        checks.append({'citation': f'[D{doc}:L{start}-L{end}]',
                       'in_retrieved_range': covers_range(observed, doc, start, end)})
    return {'status': 'no_citations' if not checks else ('locations_found' if all(c['in_retrieved_range'] for c in checks) else 'invalid_locations'),
            'checks': checks, 'semantic_support_checked': False}

