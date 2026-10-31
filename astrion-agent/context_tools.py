"""当前 Agent 档案的受限读取接口；引用不是任意文件路径。"""
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
    spec('pin_evidence', '把关键数字或结论的连续原文固定在上下文；最多 1000 字符。quote 为空取消固定，原档案仍保留。',
         {'ref': REF, 'quote': {'type': 'string'}}, ['ref', 'quote']),
]


def context_implementations(memory):
    return {spec.name: getattr(memory, spec.name) for spec in CONTEXT_SPECS}
