"""当前 Agent 档案的受限读取接口；引用不是任意文件路径。"""
import json
from tooling import ToolSpec


def spec(name, description, properties, required):
    return ToolSpec(name, description, {'type': 'object', 'properties': properties,
                                      'required': required, 'additionalProperties': False})


REF = {'type': 'string', 'minLength': 1, 'description': '工具返回的 ctx_ 引用，不能编造'}
CONTEXT_SPECS = [
    spec('read_evidence', '按引用重新读取当前 Agent 档案；按字符分页，保留来源元数据。',
         {'ref': REF, 'offset': {'type': 'integer', 'description': '非负字符偏移，默认 0'},
          'limit': {'type': 'integer', 'description': '1 到 4000，默认 2000'}}, ['ref']),
    spec('search_evidence', '在本 Agent 已观察的档案中搜索原文与引用，每页最多 5 条。',
         {'query': {'type': 'string', 'minLength': 1}, 'offset': {'type': 'integer'}}, ['query']),
    spec('search_step_history', '搜索本 Agent 的 Step 摘要和原始执行事件；摘要只在重构时生成。',
         {'query': {'type': 'string'}, 'offset': {'type': 'integer'}}, []),
    spec('read_step_history', '分页读取当前 Agent 的 Step 原始事件或已生成的摘要。',
         {'step_id': {'type': 'string'}, 'level': {'type': 'string', 'enum': ['RAW', 'SS', 'S', 'M', 'L']},
          'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['step_id']),
    spec('pin_evidence', '把关键数字或结论的连续原文固定在上下文；最多 1000 字符。quote 为空取消固定，原档案仍保留。',
         {'ref': REF, 'quote': {'type': 'string'}}, ['ref', 'quote']),
]


def context_implementations(memory):
    def search_step_history(query='', offset=0):
        if not isinstance(query, str) or not isinstance(offset, int) or offset < 0:
            raise ValueError('invalid_history_search')
        history = getattr(memory, 'agent_history', None)
        rows = []
        for step in history.steps() if history else []:
            summary = history.get_summary(step['step_id'], step['source_version'], 'SS') if step['source_version'] else None
            item = {'step_id': step['step_id'], 'status': step['status'], 'ss': summary['content'] if summary else None}
            summary_match = not query or query.casefold() in json.dumps(item, ensure_ascii=False).casefold()
            raw_match = False
            if query and not summary_match:
                events = history.step(step['step_id'])['events']
                # Request snapshots recursively contain prior contexts; search only
                # this step's observed output and action events.
                observed = [{**event, 'payload': {k: v for k, v in event['payload'].items() if k != 'request_snapshot'}}
                            for event in events]
                raw_match = query.casefold() in json.dumps(observed, ensure_ascii=False).casefold()
            if summary_match or raw_match:
                item['matched_raw'] = raw_match
                rows.append(item)
        return json.dumps({'items': rows[offset:offset + 5], 'next_offset': offset + 5 if offset + 5 < len(rows) else None}, ensure_ascii=False)

    def read_step_history(step_id, level='RAW', offset=0, limit=2000):
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 4000:
            raise ValueError('invalid_history_page')
        history = getattr(memory, 'agent_history', None)
        if not history:
            raise ValueError('history_unavailable')
        step = history.step(step_id)
        if level == 'RAW':
            # Previous model inputs are separately archived snapshots and are not
            # recursively returned in ordinary step reading.
            events = [{**e, 'payload': {k: v for k, v in e['payload'].items() if k != 'request_snapshot'}}
                      for e in step.pop('events')]
            text = json.dumps({**step, 'events': events}, ensure_ascii=False)
        elif level in ('SS', 'S', 'M', 'L'):
            summary = history.get_summary(step_id, step['source_version'], level)
            if not summary:
                raise ValueError('summary_not_generated_use_raw')
            text = summary['content']
        else:
            raise ValueError('invalid_history_level')
        return json.dumps({'step_id': step_id, 'level': level, 'offset': offset,
                           'content': text[offset:offset + limit], 'total_chars': len(text),
                           'next_offset': offset + limit if offset + limit < len(text) else None}, ensure_ascii=False)

    return {**{spec.name: getattr(memory, spec.name) for spec in CONTEXT_SPECS
              if spec.name not in ('search_step_history', 'read_step_history')},
            'search_step_history': search_step_history, 'read_step_history': read_step_history}
