"""Processing, coverage and evidence for the existing authenticated video source."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from dovideo.application.edit_timeline import missing
from dovideo.infrastructure.edit_captions import parse_srt
from dovideo.infrastructure.persistence.processing_worker import ProcessingWorker
from dovideo.infrastructure.persistence.source_tables import sources as sources_table
from .schemas import CancelRequest, ProcessingCreate


def install_processing(app, require_user):
    sources = app.state.edit_sources
    worker = app.state.processing = ProcessingWorker(sources)
    sources.enqueue_completed = worker.register_upload
    catalog = worker.catalog

    def resolve_track(workspace, revision, track_id):
        row = catalog.get_record(workspace, track_id, "ARTIFACT")
        if (
            row is None
            or row.unit != "aligned-captions"
            or row.body["artifact"]["media_revision_id"] != revision
        ):
            raise missing()
        return parse_srt(catalog.derived_path(row.body["file"]).read_text(encoding="utf-8"))

    app.state.edit_worker.repo.resolve_track = resolve_track
    api = APIRouter(prefix="/v1")

    def scope(user=Depends(require_user)):
        return sources.workspace(user)

    @api.post("/assets/{asset_id}/processing-runs", status_code=202)
    def enqueue(asset_id: UUID, body: ProcessingCreate, workspace=Depends(scope)):
        return worker.enqueue(workspace, str(asset_id), body)

    @api.get("/assets/{asset_id}/processing-runs")
    def history(
        asset_id: UUID,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        workspace=Depends(scope),
    ):
        return catalog.page(workspace, str(asset_id), "RUN", cursor, limit)

    @api.get("/processing-runs/{processing_run_id}")
    def detail(processing_run_id: UUID, workspace=Depends(scope)):
        return catalog.get(workspace, str(processing_run_id))

    @api.get("/processing-runs/{processing_run_id}/usage")
    def usage(processing_run_id: UUID, workspace=Depends(scope)):
        return catalog.visual_usage(workspace, str(processing_run_id))

    @api.post("/processing-runs/{processing_run_id}/cancel", status_code=202)
    def cancel(processing_run_id: UUID, body: CancelRequest, workspace=Depends(scope)):
        return catalog.cancel(workspace, str(processing_run_id))

    @api.get("/assets/{asset_id}/coverage")
    def coverage(
        asset_id: UUID,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        workspace=Depends(scope),
    ):
        return catalog.page(workspace, str(asset_id), "COVERAGE", cursor, limit)

    @api.get("/assets/{asset_id}/observations")
    def observations(
        asset_id: UUID,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        workspace=Depends(scope),
    ):
        return catalog.page(workspace, str(asset_id), "OBSERVATION", cursor, limit)

    @api.get("/assets/{asset_id}/notes")
    def notes(asset_id: UUID, workspace=Depends(scope)):
        asset = sources._one(sources_table, workspace, sources_table.c.id, str(asset_id))
        rid = (asset.probe or {}).get("notes_artifact_id")
        if not rid:
            raise HTTPException(409, "观影笔记尚未生成")
        return dict(artifact_id=rid, media_revision_id=asset.revision)

    @api.get("/assets/{asset_id}/caption-track")
    def caption_track(asset_id: UUID, workspace=Depends(scope)):
        asset = sources._one(sources_table, workspace, sources_table.c.id, str(asset_id))
        return dict(
            caption_track_id=(asset.probe or {}).get("caption_track_id"), media_revision_id=asset.revision
        )

    @api.get("/observations/{observation_id}")
    def observation(observation_id: UUID, workspace=Depends(scope)):
        row = catalog.get_record(workspace, str(observation_id), "OBSERVATION")
        if row is None:
            raise HTTPException(404, "观察不存在")
        return row.body

    @api.get("/evidence/{evidence_id}")
    def evidence(evidence_id: UUID, workspace=Depends(scope)):
        workflow = getattr(app.state, "workflow_worker", None)
        if workflow and (verified := workflow.evidence(workspace, str(evidence_id))) is not None:
            return verified
        verified_search = getattr(getattr(app.state, "evidence_search", None), "verified_evidence", None)
        if verified_search and (found := verified_search(workspace, str(evidence_id))) is not None:
            return found
        return catalog.evidence(workspace, str(evidence_id))

    @api.delete("/assets/{asset_id}", status_code=202)
    def remove(asset_id: UUID, workspace=Depends(scope)):
        return catalog.delete_asset(workspace, str(asset_id))

    app.include_router(api)
