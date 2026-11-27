"""Workspace-scoped revision and render endpoints for the editing workbench."""
from functools import partial
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse

from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.application.render_formats import EXPORT_FORMATS
from cutsieve.infrastructure.persistence.edit_repository import EditRepository, job_view, project_view
from cutsieve.infrastructure.persistence.edit_tables import jobs, projects, revisions
from cutsieve.infrastructure.persistence.render_worker import RenderWorker
from .edit_sources import signature
from .schemas import CancelRequest, EditProjectCreate, RenderJobCreate, TimelinePatch


def install_edit_routes(app, workspace_user):
    sources = app.state.edit_sources
    repo = EditRepository(sources)
    worker = app.state.edit_worker = RenderWorker(repo, partial(signature, sources.root, "edit_artifact_content"))
    router = APIRouter(prefix="/v1")

    @app.exception_handler(EditError)
    async def edit_error(_request, exc):
        return JSONResponse(dict(code=exc.code, message=str(exc), retryable=exc.status == 503,
                                 request_id=uid(), details={}), status_code=exc.status)

    def workspace(user=Depends(workspace_user)):
        return sources.workspace(user)

    @router.post("/edit-projects", status_code=201)
    def create(body: EditProjectCreate, scope=Depends(workspace)):
        return repo.create(scope, body)

    @router.get("/projects/{project_id}/edit-projects")
    def list_edits(project_id: str, scope=Depends(workspace), cursor: str | None=None,
                   limit: int=Query(20, ge=1, le=100)):
        sources.project(scope, project_id)
        return repo.page(projects, scope, project_view, dict(project_id=project_id), cursor, limit)

    @router.get("/edit-projects/{edit_project_id}")
    def get_edit(edit_project_id: str, scope=Depends(workspace)):
        return repo.get(projects, scope, edit_project_id, project_view)

    @router.get("/edit-projects/{edit_project_id}/revisions")
    def list_revisions(edit_project_id: str, scope=Depends(workspace), cursor: str | None=None,
                       limit: int=Query(20, ge=1, le=100)):
        repo.get(projects, scope, edit_project_id, project_view)
        return repo.page(revisions, scope, lambda r: r.snapshot, dict(edit_id=edit_project_id), cursor, limit)

    @router.get("/edit-projects/{edit_project_id}/revisions/{revision_id}")
    def get_revision(edit_project_id: str, revision_id: str, scope=Depends(workspace)):
        return repo.get(revisions, scope, revision_id, lambda r: r.snapshot, edit_id=edit_project_id)

    @router.post("/edit-projects/{edit_project_id}/operations", status_code=201)
    def patch(edit_project_id: str, body: TimelinePatch, scope=Depends(workspace)):
        result = repo.patch(scope, edit_project_id, body)
        return JSONResponse(result, status_code=200 if result["replayed"] else 201)

    @router.post("/render-jobs", status_code=202)
    def render(body: RenderJobCreate, scope=Depends(workspace),
               export_format: str=Query("mp4", alias="format", json_schema_extra={"enum": list(EXPORT_FORMATS)})):
        return repo.enqueue(scope, body, export_format=export_format)

    @router.get("/render-jobs/{render_job_id}")
    def get_job(render_job_id: str, scope=Depends(workspace)):
        return repo.get(jobs, scope, render_job_id, job_view)

    @router.get("/edit-projects/{edit_project_id}/render-jobs")
    def list_jobs(edit_project_id: str, scope=Depends(workspace), cursor: str | None=None,
                  limit: int=Query(20, ge=1, le=100)):
        repo.get(projects, scope, edit_project_id, project_view)
        return repo.page(jobs, scope, job_view, dict(edit_id=edit_project_id), cursor, limit)

    @router.post("/render-jobs/{render_job_id}/cancel", status_code=202)
    def cancel(render_job_id: str, body: CancelRequest, scope=Depends(workspace)):
        return repo.cancel(scope, render_job_id, body)

    @router.get("/edit-artifacts/{artifact_id}/content", name="edit_artifact_content")
    def content(artifact_id: str, w: str, expires: int, sig: str):
        path, mime = worker.download(w, artifact_id, expires, sig)
        return FileResponse(path, media_type=mime, headers={"Cache-Control":"private, no-store"})

    app.include_router(router)
