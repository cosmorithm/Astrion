"""Research capability assembly. Core loop owns control implementations."""

from .builtin import TOOL_SPECS, finish, ask_user
from .knowledge import KNOWLEDGE_SPECS, KnowledgeTools

from .contracts import spec, S
from .computation import COMPUTATION_SPECS, ComputationTools
from .external import EXTERNAL_SPECS, ExternalTools

CONTROL_SPECS = [s for s in TOOL_SPECS if s.name in {"finish", "ask_user", "update_task_plan"}]
DELEGATE = spec(
    "run_worker",
    "把明确子任务交给独立子 Agent；子 Agent 不可再次委派。",
    {"task": S, "context": {"type": "string"}},
    ["task", "context"],
)
DELEGATE_BATCH = spec(
    "run_workers",
    "并发执行一批相互独立的子任务并等待全部返回；每次研究累计最多 5 个子 Agent，子 Agent 不可再委派。"
    "返回摘要、状态及报告产物 ID；摘要截断时用 read_project_file 重读。",
    {"tasks": {"type": "array", "minItems": 1, "items": {
        "type": "object", "properties": {"task": S, "context": {"type": "string"}},
        "required": ["task", "context"], "additionalProperties": False}}},
    ["tasks"],
)


def build_tools(store, project, *, worker=False, delegate=None, batch_delegate=None):
    bound = KnowledgeTools(store, project)
    specs = [*KNOWLEDGE_SPECS, *EXTERNAL_SPECS, *COMPUTATION_SPECS, *CONTROL_SPECS]
    implementations = {
        **bound.implementations(),
        **ExternalTools(store, project).implementations(),
        **ComputationTools(store, project).implementations(),
        "finish": finish,
        "ask_user": ask_user,
    }
    if not worker:
        specs.append(DELEGATE)
        implementations["run_worker"] = delegate
        if batch_delegate is not None:
            specs.append(DELEGATE_BATCH)
            implementations["run_workers"] = batch_delegate
    return specs, implementations
