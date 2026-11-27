"""Authenticated workflow controls, immutable output, and resumable SSE."""

import asyncio
import json
from functools import partial
import os
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from cutsieve.application.conversation_runs import conversation
from cutsieve.application.edit_timeline import EditError
from cutsieve.application.workflow_state import DEFAULT_BUDGET, TERMINAL
from cutsieve.application.workflow_tools import job_status
from cutsieve.infrastructure.persistence.workflow_repository import WorkflowRepository
from cutsieve.infrastructure.persistence.workflow_worker import WorkflowWorker
from cutsieve.infrastructure.persistence.work_outbox import WorkOutbox
from .edit_sources import signature
from .schemas import Budget, BudgetAdjust, CancelRequest, IdempotencyKey, SearchWorkflow, WorkflowCreate, WorkflowResume


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    media_revision_id: UUID
    parent_run_id: UUID | None = None
    idempotency_key: IdempotencyKey
    budget: Budget | None = None


def install_workflow_routes(app, require_user):
    sources = app.state.edit_sources
    edits = app.state.edit_worker.repo
    repo = WorkflowRepository(edits)
    worker = app.state.workflow_worker = WorkflowWorker(
        repo, app, partial(signature, sources.root, "workflow-result")
    )
    app.state.work_outbox = WorkOutbox(edits)

    def resolve_evidence(workspace, eid, *, session=None):
        value = worker.evidence(workspace, eid, session=session)
        if value is not None:
            return value
        search = getattr(app.state, "evidence_search", None)
        if search is not None:
            value = search.verified_evidence(workspace, eid)
            if value is not None:
                return value
        service = getattr(app.state, "processing", None)
        if service is None:
            raise EditError(503, "CAPABILITY_UNAVAILABLE", "媒体索引暂不可用")
        return service.catalog.evidence(workspace, eid)

    edits.resolve_evidence = resolve_evidence
    router = APIRouter(prefix="/v1")

    def workspace(user=Depends(require_user)):
        return sources.workspace(user)

    def require_enabled():
        if os.getenv("CUTSIEVE_WORKFLOWS_ENABLED", "true").lower() in ("0", "false"):
            raise EditError(503, "WORKFLOWS_DISABLED", "新的工作流已停用，历史运行仍可查看")

    @router.post("/workflow-runs", status_code=202)
    def create(body: WorkflowCreate, scope=Depends(workspace)):
        require_enabled()
        return repo.create(scope, body)

    @router.post("/projects/{project_id}/conversation-runs", status_code=202)
    def start_conversation(project_id: str, turn: ChatTurn, scope=Depends(workspace)):
        require_enabled()
        body = dict(
            intent="AUTO",
            message=turn.message,
            idempotency_key=turn.idempotency_key,
            scope=dict(project_id=project_id, media_revision_ids=[str(turn.media_revision_id)]),
        )
        if turn.budget is not None:
            body["budget"] = turn.budget
        elif limit := os.getenv("CUTSIEVE_CHAT_MAX_COST_MICROUNITS"):
            amount = int(limit)
            if amount < 0:
                raise EditError(422, "INVALID_REQUEST", "费用上限需要为非负整数")
            body["budget"] = dict(DEFAULT_BUDGET, max_cost_microunits=amount)
        context = dict(
            parent_run_id=str(turn.parent_run_id) if turn.parent_run_id else None,
        )
        return repo.create(scope, body, conversation=context)

    @router.get("/workflow-runs/{run_id}/conversation")
    def read_conversation(run_id: str, scope=Depends(workspace)):
        return conversation(repo, scope, run_id)

    @router.post("/projects/{project_id}/search-workflows", status_code=202)
    def freeze_search(project_id: str, body: SearchWorkflow, scope=Depends(workspace)):
        require_enabled()
        query = body["search"]
        sources.project(scope, project_id)
        if query["scope"]["project_id"] != project_id:
            raise EditError(404, "NOT_FOUND", "资源不存在")
        image = query.get("image_artifact_id")
        text = (query.get("text") or "").strip()
        if image:
            resource = app.state.evidence_search.files.get(scope, image, project=project_id)
            if not resource or resource.kind != "IMAGE_QUERY":
                raise EditError(404, "NOT_FOUND", "资源不存在")
        run = dict(
            scope=query["scope"],
            intent="QUESTION",
            message=text or "核查查询截图",
            idempotency_key=body["idempotency_key"],
        )
        return repo.create(scope, run, search_request=query)

    @router.get("/projects/{project_id}/workflow-runs")
    def history(
        project_id: str,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        scope=Depends(workspace),
    ):
        sources.project(scope, project_id)
        return repo.history(scope, project_id, cursor, limit)

    @router.get("/workflow-runs/{run_id}")
    def snapshot(run_id: str, scope=Depends(workspace)):
        return repo.get(scope, run_id)

    @router.get("/workflow-runs/{run_id}/harness")
    def harness_view(run_id: str, offset: int = Query(0, ge=0), scope=Depends(workspace)):
        return repo.harness_view(scope, run_id, offset)

    @router.post("/workflow-runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, body: WorkflowResume, scope=Depends(workspace)):
        return repo.control(scope, run_id, "resume", body)

    @router.patch("/workflow-runs/{run_id}/budget")
    def adjust(run_id: str, body: BudgetAdjust, scope=Depends(workspace)):
        return repo.control(scope, run_id, "budget", body)

    @router.post("/workflow-runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str, body: CancelRequest, scope=Depends(workspace)):
        return repo.control(scope, run_id, "cancel", body)

    @router.get("/workflow-runs/{run_id}/result")
    def result(run_id: str, scope=Depends(workspace)):
        state = repo.get(scope, run_id)
        key = state["result_artifact_id"]
        if not key:
            raise EditError(409, "RESULT_PENDING", "任务尚无最终产物")
        return repo.document(scope, key).body

    @router.get("/workflow-runs/{run_id}/jobs/{job_id}")
    def child_job(run_id: str, job_id: str, scope=Depends(workspace)):
        with repo.sessions() as db:
            row = repo.readable(db, scope, run_id)
            return job_status(repo, app.state.evidence_search, row, job_id)

    @router.get("/workflow-artifacts/{key}/content", name="workflow_artifact_bytes")
    def document_content(key: str, workspace: str, expires: int, token: str):
        return Response(
            worker.content(workspace, key, expires, token),
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/workflow-runs/{run_id}/events")
    def stream(
        run_id: str,
        request: Request,
        after: str | None = None,
        last_event_id: str | None = Header(None),
        scope=Depends(workspace),
    ):
        start = repo.event_cursor(scope, run_id, last_event_id or after)

        async def follow():
            sequence = start
            idle = 0
            while not await request.is_disconnected():
                snapshot = await asyncio.to_thread(repo.get, scope, run_id)
                page = await asyncio.to_thread(repo.page, scope, run_id, str(sequence), 100)
                for event in page["items"]:
                    sequence = event["sequence"]
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event['event_id']}\nevent: {event['type'].lower()}\ndata: {data}\n\n"
                if sequence >= snapshot["last_event_sequence"] and snapshot["status"] in TERMINAL | {
                    "AWAITING_INPUT"
                }:
                    return
                if not page["items"]:
                    idle += 1
                    if idle % 20 == 0:
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            follow(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(router)
