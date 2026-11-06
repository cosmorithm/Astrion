"""Project-scoped research history and report versions."""

from fastapi import APIRouter, Request
from .dependencies import database
from ..runs import RunRepository

router = APIRouter(prefix="/api/projects/{project_id}/runs")


@router.get("")
def listing(project_id: int, request: Request, offset: int = 0, limit: int = 20):
    with database(request) as store:
        return RunRepository(store, project_id).listing(offset, limit)


@router.get("/graph")
def graph(project_id: int, request: Request, limit: int = 200):
    from ..run_graph import version_graph
    with database(request) as store:
        return version_graph(RunRepository(store, project_id), limit)


@router.get("/{run_id}")
def detail(project_id: int, run_id: str, request: Request):
    with database(request) as store:
        return RunRepository(store, project_id).get(run_id)


@router.delete("/{run_id}")
def delete_failed(project_id: int, run_id: str, request: Request):
    with database(request) as store:
        return RunRepository(store, project_id).delete_failed(run_id)


@router.get("/{run_id}/citation-visibility")
def citation_visibility(project_id: int, run_id: str, request: Request):
    """Read-only, bounded replay of final-request visibility for saved reports."""
    import json
    from ..citations import audit_citations
    with database(request) as store:
        repo = RunRepository(store, project_id)
        result = repo.get(run_id)  # Verify database, project, identity and safe path first.
        answer = (result.get("result") or {}).get("answer")
        path = repo.directory(run_id) / "events.jsonl"
        limit = 32 * 1024 * 1024
        reason = None
        trace = []
        if path.is_symlink():
            raise ValueError("research_run_path_denied")
        if not path.is_file():
            reason = "event_log_missing"
        else:
            try:
                with path.open("rb") as handle:
                    data = handle.read(limit + 1)
                if len(data) > limit:
                    reason = "event_log_too_large"
                else:
                    trace = [json.loads(line) for line in data.splitlines() if line.strip()]
                    if any(not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                           or not isinstance(item["event"].get("type"), str)
                           or not isinstance(item["event"].get("payload", {}), dict) for item in trace):
                        raise ValueError("invalid_event")
            except (OSError, UnicodeError, ValueError, TypeError):
                trace, reason = [], "event_log_invalid"
        try:
            audit = audit_citations(answer if isinstance(answer, str) else None, trace)
        except (ValueError, TypeError, KeyError, AttributeError):
            reason = "event_log_invalid"
            audit = audit_citations(answer if isinstance(answer, str) else None, [])
        return {"run_id": run_id, "reason": reason,
                "checks": [{"citation": c["citation"], "model_visibility": c["model_visibility"]}
                           for c in audit["checks"]]}


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
