"""本机研究工作台 API：HTTP 管理数据，SSE 返回研究事件和模型增量。"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent
from .runner import run_research
from .store import Store
from .streaming import StreamingChatLLM
from .config import DEFAULT_STEPS, DEFAULT_WORKERS, MAX_WORKERS
from .continuation import load_parent

agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
app = FastAPI(title="Astrion Research API")
DB_PATH = Path(
    os.environ.get(
        "ASTRION_RESEARCH_DB",
        Path(__file__).resolve().parent.parent / "research_data" / "astrion.sqlite3",
    )
)

app.state.store_factory = lambda: Store(DB_PATH)
from .routes.library import router as library_router

app.include_router(library_router)
from .routes.external import router as external_router

app.include_router(external_router)
from .routes.artifacts import router as artifacts_router

app.include_router(artifacts_router)
from .routes.runs import router as runs_router

app.include_router(runs_router)


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class DocumentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_000_000)


class MemoryInput(BaseModel):
    scope: str
    content: str = Field(min_length=1, max_length=10000)
    project_id: int | None = None


class RunInput(BaseModel):
    project_id: int
    task: str = Field(min_length=1, max_length=10000)
    max_steps: int = Field(default=DEFAULT_STEPS, ge=0, le=DEFAULT_STEPS)
    max_workers: int = Field(default=DEFAULT_WORKERS, ge=0, le=MAX_WORKERS)
    worker_steps: int = Field(default=DEFAULT_STEPS, ge=1, le=DEFAULT_STEPS)
    continue_run_id: str | None = Field(default=None, pattern=r"^research_[a-f0-9]{32}$")


def run_store(fn):
    store = Store(DB_PATH)
    try:
        return fn(store)
    except (ValueError, sqlite3.IntegrityError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        store.close()


@app.get("/api/projects")
def projects():
    return run_store(lambda s: s.projects())


@app.post("/api/projects")
def create_project(body: ProjectInput):
    return run_store(lambda s: {"id": s.create_project(body.name)})


@app.get("/api/projects/{project_id}/documents")
def documents(project_id: int):
    return run_store(lambda s: s.documents(project_id))


@app.post("/api/projects/{project_id}/documents")
def add_document(project_id: int, body: DocumentInput):
    return run_store(lambda s: {"id": s.add_document_text(project_id, body.name, body.content)})


@app.get("/api/memories")
def memories(project_id: int | None = None):
    return run_store(lambda s: s.memories(project_id))


@app.post("/api/memories")
def add_memory(body: MemoryInput):
    return run_store(lambda s: {"id": s.add_memory(body.scope, body.content, body.project_id)})


@app.post("/api/memories/{memory_id}/forget")
def forget_memory(memory_id: int):
    return run_store(lambda s: {"forgotten": s.forget(memory_id)})


def sse(value):
    return "data: " + json.dumps(value, ensure_ascii=True) + "\n\n"


@app.get("/api/research/config")
def research_config():
    return {"max_steps": DEFAULT_STEPS, "worker_steps": DEFAULT_STEPS, "max_workers": DEFAULT_WORKERS,
            "limits": {"steps": DEFAULT_STEPS, "workers": MAX_WORKERS}}


@app.post("/api/research/stream")
def research_stream(body: RunInput):
    if not body.task.strip():
        raise HTTPException(status_code=400, detail="研究问题不能为空")
    run_store(lambda s: s.project(body.project_id))
    if body.continue_run_id:
        run_store(lambda s: load_parent(s, body.project_id, body.continue_run_id))
    agent.load_env(agent.find_env_files(Path(agent.__file__).resolve().parent))
    key = agent.first_nonempty(
        os.environ.get("AGENT_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
    )
    if not key:
        raise HTTPException(status_code=400, detail="未配置模型 API key")
    messages = queue.Queue()

    def emit(value):
        messages.put(value)

    def worker():
        store = None
        try:
            store = Store(DB_PATH)
            model = StreamingChatLLM(
                key,
                os.environ.get("AGENT_BASE_URL") or "https://api.deepseek.com",
                os.environ.get("AGENT_MODEL") or "deepseek-chat",
                emit,
            )
            result = run_research(
                store,
                body.project_id,
                body.task,
                model,
                max_steps=body.max_steps,
                max_workers=body.max_workers,
                worker_steps=body.worker_steps,
                continue_run_id=body.continue_run_id,
                on_event=lambda item: emit({"type": "event", **item}),
            )
            emit({"type": "done", "result": {k: v for k, v in result.items() if k != "trace"}})
        except BaseException as exc:
            # 不把服务端 traceback、凭据或上游错误响应转给浏览器。
            print(f"研究运行失败：{type(exc).__name__}", file=sys.stderr, flush=True)
            emit(
                {
                    "type": "error",
                    "message": f"研究运行失败（{type(exc).__name__}），请检查后端日志与模型配置。",
                }
            )
        finally:
            if store is not None:
                store.close()
            messages.put(None)

    def stream():
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            try:
                value = messages.get(timeout=10)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if value is None:
                break
            yield sse(value)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
