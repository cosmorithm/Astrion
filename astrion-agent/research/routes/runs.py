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
