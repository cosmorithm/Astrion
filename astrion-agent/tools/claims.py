"""Research-only tool declarations for auditable claim/source links."""
from research.claims import ClaimLedger
from .contracts import dump, spec

CLAIM_SPECS = [
    spec('record_claim', '登记一条准备写入报告的简短结论及支持原文；引用必须来自本 Agent 工具结果中的 evidence_refs，quote 须逐字复制。程序确定版本、页码和行号；登记不等于证明结论正确。',
         {'statement': {'type': 'string', 'minLength': 1, 'description': '最多 300 字符'},
          'sources': {'type': 'array', 'minItems': 1, 'description': '1–3 条证据', 'items': {
              'type': 'object', 'properties': {'ref': {'type': 'string', 'minLength': 1},
                                               'quote': {'type': 'string', 'minLength': 1, 'description': '最多 500 字符'}},
              'required': ['ref', 'quote'], 'additionalProperties': False}}}, ['statement', 'sources']),
    spec('list_claims', '分页查看本 Agent 已登记结论及原始证据；每页最多 3 条。',
         {'offset': {'type': 'integer'}, 'limit': {'type': 'integer', 'description': '1–3'}}, []),
]


def implementations(memory):
    ledger = ClaimLedger(memory)
    return {'record_claim': lambda statement, sources: dump(ledger.record(statement, sources)),
            'list_claims': lambda offset=0, limit=3: dump(ledger.list(offset, limit))}
