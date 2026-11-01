"""Project-scoped artifact upload, preview, download and computation endpoints."""

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field
from urllib.parse import quote

from .dependencies import database
from ..storage.artifacts import Artifacts, MAX_FILE
from ..execution.service import Computation

router = APIRouter(prefix="/api/projects/{project_id}")


class ExecutionInput(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    timeout: int = Field(default=20, ge=1, le=60)


@router.get("/artifacts")
def listing(project_id: int, request: Request, offset: int = 0, limit: int = 20):
    with database(request) as store:
        return Artifacts(store).listing(project_id, offset, limit)


@router.post("/artifacts")
def upload(project_id: int, request: Request, file: UploadFile = File(...)):
    with database(request) as store:
        data = file.file.read(MAX_FILE + 1)
        return Artifacts(store).put(
            project_id, file.filename, data, provenance={"kind": "user_upload"}
        )


@router.get("/artifacts/{artifact_id}/read")
def preview(
    project_id: int, artifact_id: str, request: Request, offset: int = 0, limit: int = 4000
):
    with database(request) as store:
        return Artifacts(store).read(project_id, artifact_id, offset, limit)


@router.get("/artifacts/{artifact_id}/download")
def download(project_id: int, artifact_id: str, request: Request):
    with database(request) as store:
        repository = Artifacts(store)
        item = repository.get(project_id, artifact_id)
        data = repository.bytes(project_id, artifact_id)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(item["name"], safe=""),
                "X-Content-Type-Options": "nosniff",
            },
        )


@router.post("/execute")
def execute(project_id: int, body: ExecutionInput, request: Request):
    with database(request) as store:
        return Computation(store, project_id).execute(
            body.code, body.input_artifact_ids, body.timeout
        )


@router.get("/artifacts/{artifact_id}/image")
def image(project_id: int, artifact_id: str, request: Request):
    with database(request) as store:
        data = Artifacts(store).bytes(project_id, artifact_id)
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            media = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            media = "image/jpeg"
        else:
            raise ValueError("image_preview_requires_png_or_jpeg")
        return Response(
            data,
            media_type=media,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )


@router.get("/execution-status")
def status(project_id: int, request: Request):
    with database(request) as store:
        return Computation(store, project_id).executor.availability()
