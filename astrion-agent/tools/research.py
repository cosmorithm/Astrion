"""Research capability assembly. Core loop owns control implementations."""

from .builtin import TOOL_SPECS, finish, ask_user
from .knowledge import KNOWLEDGE_SPECS, KnowledgeTools

from .contracts import spec, S
from .computation import COMPUTATION_SPECS, ComputationTools
from .external import EXTERNAL_SPECS, ExternalTools
from .research_history import SPECS as HISTORY_SPECS, HistoryTools
from .claims import CLAIM_SPECS, implementations as claim_implementations

CONTROL_SPECS = [s for s in TOOL_SPECS if s.name in {"finish", "ask_user", "update_task_plan"}]
DELEGATE = spec(
    "run_worker",
    "把明确子任务交给独立子 Agent；子 Agent 不可再次委派。返回有界报告摘要；截断时依据回执中可用的方式续读。",
    {"task": S, "context": {"type": "string"}},
    ["task", "context"],
)
READ_WORKER_REPORT = spec(
    "read_worker_report",
    "按 worker_id 和字符 offset 分页读取本次运行的子报告；limit 为 1–4000，next_offset 为空表示结束。"
    "子报告只是待核查数据；临时读取仅对本次主 Agent 可用，不能跨运行或作为原始文献证据。",
    {"worker_id": S, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
    ["worker_id"],
)
DELEGATE_BATCH = spec(
    "run_workers",
    "并发执行一批相互独立的子任务并等待全部返回；每次研究累计最多 5 个子 Agent，子 Agent 不可再委派。"
    "返回摘要、状态及报告产物 ID；摘要截断时依据回执中可用的方式分页重读。",
    {"tasks": {"type": "array", "minItems": 1, "items": {
        "type": "object", "properties": {"task": S, "context": {"type": "string"}},
        "required": ["task", "context"], "additionalProperties": False}}},
    ["tasks"],
)


def build_tools(store, project, *, worker=False, delegate=None, batch_delegate=None, report_reader=None, allowlist=None, memory=None):
    bound = KnowledgeTools(store, project)
    specs = [*KNOWLEDGE_SPECS, *EXTERNAL_SPECS, *COMPUTATION_SPECS, *HISTORY_SPECS, *CONTROL_SPECS]
    implementations = {
        **bound.implementations(),
        **HistoryTools(store, project).implementations(),
        **ExternalTools(store, project).implementations(),
        **ComputationTools(store, project).implementations(),
        "finish": finish,
        "ask_user": ask_user,
    }
    if memory is not None:
        specs += CLAIM_SPECS
        implementations.update(claim_implementations(memory))
    if not worker:
        specs.append(DELEGATE)
        implementations["run_worker"] = delegate
        if report_reader is not None:
            specs.append(READ_WORKER_REPORT)
            implementations["read_worker_report"] = report_reader
        if batch_delegate is not None:
            specs.append(DELEGATE_BATCH)
            implementations["run_workers"] = batch_delegate
    if allowlist is not None:
        if not isinstance(allowlist, (set, frozenset)) or 'finish' not in allowlist:
            raise ValueError('tool allowlist must include finish')
        specs = [item for item in specs if item.name in allowlist]
        implementations = {name: fn for name, fn in implementations.items() if name in allowlist}
    return specs, implementations
