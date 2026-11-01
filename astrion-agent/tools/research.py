"""Research capability assembly. Core loop owns control implementations."""
from .builtin import TOOL_SPECS, finish, ask_user
from .knowledge import KNOWLEDGE_SPECS, KnowledgeTools, spec, S

from .external import EXTERNAL_SPECS, ExternalTools

CONTROL_SPECS=[s for s in TOOL_SPECS if s.name in {'finish','ask_user','update_task_plan'}]
DELEGATE=spec('run_worker','把明确子任务交给独立子 Agent；子 Agent 不可再次委派。',{'task':S,'context':{'type':'string'}},['task','context'])


def build_tools(store,project,*,worker=False,delegate=None):
    bound=KnowledgeTools(store,project)
    specs=[*KNOWLEDGE_SPECS,*EXTERNAL_SPECS,*CONTROL_SPECS]
    implementations={**bound.implementations(),**ExternalTools(store,project).implementations(),'finish':finish,'ask_user':ask_user}
    if not worker:
        specs.append(DELEGATE);implementations['run_worker']=delegate
    return specs,implementations
