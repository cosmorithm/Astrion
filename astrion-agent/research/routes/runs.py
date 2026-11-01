"""Project-scoped research history and report versions."""

from fastapi import APIRouter, Request
from .dependencies import database
from ..runs import RunRepository

router = APIRouter(prefix="/api/projects/{project_id}/runs")


@router.get("")
def listing(project_id: int, request: Request, offset: int = 0, limit: int = 20):
    with database(request) as store:
        return RunRepository(store, project_id).listing(offset, limit)


@router.get("/{run_id}")
def detail(project_id: int, run_id: str, request: Request):
    with database(request) as store:
        return RunRepository(store, project_id).get(run_id)


@router.get("/{run_id}/events")
def events(project_id: int, run_id: str, request: Request, offset: int = 0, limit: int = 10):
    with database(request) as store:
        return RunRepository(store, project_id).events_page(run_id, offset, limit)


@router.get("/{run_id}/agents")
def run_agents(project_id: int, run_id: str, request: Request):
    from ..context_history import agents
    with database(request) as store:
        return {"agents": agents(store, project_id, run_id)}


@router.get("/{run_id}/agents/{agent_id}/steps")
def agent_steps(project_id: int, run_id: str, agent_id: str, request: Request,
                step_id: str | None = None, offset: int = 0, limit: int = 20):
    from ..context_history import history
    with database(request) as store:
        return history(store, project_id, run_id, agent_id, step_id=step_id, offset=offset, limit=limit)
