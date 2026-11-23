"""Poll the persistent queue. One claim executes one bounded stage outside the SQL transaction."""

import asyncio
from copy import copy
from hashlib import sha256
import hmac
import json
import logging
import os
import time
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from dovideo.application.conversation_runs import candidate_cards, route_turn
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.workflow_state import NeedsInput, public_error, utc
from dovideo.application.workflow_steps import WorkflowSteps
from dovideo.application.workflow_roles import review_handoff
from dovideo.application.workflow_inspections import WaitInspection, pending_inspection, run_inspection
from dovideo.infrastructure.providers.workflow_flash import FlashPlanner
from .workflow_repository import StaleLease

log = logging.getLogger(__name__)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class WorkflowWorker:
    def __init__(self, repo, app, sign):
        self.repo, self.app, self.sign = repo, app, sign
        self.planner = FlashPlanner()
        self.worker_id = uid()
        self.task = None

    @property
    def team_enabled(self):
        return os.getenv("DOVIDEO_WORKFLOW_TEAM", "").lower() in ("1", "true")

    async def start(self):
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self.serve())

    async def stop(self):
        if self.task:
            self.stop_event.set()
            await self.task
            self.task = None

    async def serve(self):
        while not self.stop_event.is_set():
            try:
                claim = await asyncio.to_thread(self.repo.claim, self.worker_id)
                if claim:
                    task = asyncio.create_task(asyncio.to_thread(self.run_stage, claim))
                    while not task.done():
                        done, _ = await asyncio.wait((task,), timeout=15)
                        if not done:
                            try:
                                await asyncio.to_thread(self.repo.renew, claim)
                            except StaleLease:
                                pass
                    await task
                    continue
            except SQLAlchemyError:
                log.exception("Workflow queue unavailable")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.45)
            except TimeoutError:
                pass

    def run_stage(self, claim):
        planner = self.planner
        if "conversation" in claim.checkpoint:
            route = claim.checkpoint.get("route_decision") or {}
            enabled = planner.env.get(
                "DOVIDEO_CHAT_LIVE_MODEL", planner.env.get("DOVIDEO_WORKFLOW_LIVE_MODEL", "0")
            )
            funded = claim.state["budget_usage"]["limits"]["max_cost_microunits"] > 0
            needs_model = (
                claim.checkpoint["intent"] == "AUTO"
                or "INSPECT" in route.get("plan", [])
                or route.get("source") == "MODEL"
            )
            planner = copy(planner)
            planner.env = dict(
                planner.env, DOVIDEO_WORKFLOW_LIVE_MODEL=enabled if funded and needs_model else "0"
            )
        service = WorkflowSteps(
            self.repo,
            getattr(self.app.state, "evidence_search", None),
            planner,
            getattr(self.app.state, "edit_worker", None),
        )
        try:
            if claim.checkpoint["intent"] == "AUTO":
                outcome = route_turn(self.repo, planner, claim)
            elif job := pending_inspection(claim):
                outcome = run_inspection(self.repo, planner, service.render_worker, claim, job)
            else:
                outcome = getattr(service, claim.state["phase"].lower())(claim)
                outcome = review_handoff(service, claim, outcome)
            result = outcome.get("docs", {}).get("result", {})
            if "conversation" in claim.checkpoint and "claims" in result:
                (media_id,) = claim.checkpoint["scope"]["media_revision_ids"]
                outcome.setdefault("checkpoint", {})["conversation_candidates"] = candidate_cards(
                    self.repo, claim.workspace_id, result["claims"], media_id
                )
        except WaitInspection as wait:
            outcome = wait.outcome
        except StaleLease:
            self.repo.stopped(claim)
            return
        except NeedsInput as exc:
            outcome = dict(state=dict(status="AWAITING_INPUT", awaiting_input=exc.input), event="NEEDS_INPUT")
        except (EditError, HTTPException) as exc:
            if isinstance(exc, EditError):
                code, message, retry = exc.code, str(exc), exc.status == 503
            else:
                code, message, retry = "RESOURCE_UNAVAILABLE", str(exc.detail), exc.status_code == 503
            outcome = dict(
                state=dict(status="FAILED", pending_job_ids=[], error=public_error(code, message, retry)),
                event="FAILED",
            )
        except Exception:
            log.exception("Workflow stage failed: %s / %s", claim.id, claim.state["phase"])
            outcome = dict(
                state=dict(
                    status="FAILED", error=public_error("STAGE_FAILED", "该阶段执行失败，可以重试", True)
                ),
                event="FAILED",
            )
        try:
            self.repo.advance(claim, **outcome)
        except StaleLease:
            self.repo.stopped(claim)

    def artifact(self, workspace, key, request):
        row = self.repo.document(workspace, key)
        if row is None or row.kind == "EVIDENCE":
            return None
        data = json_bytes(row.body)
        expires = int(time.time()) + 300
        url = (
            str(request.url_for("workflow_artifact_bytes", key=key))
            + "?"
            + urlencode(dict(workspace=workspace, expires=expires, token=self.sign(workspace, key, expires)))
        )
        return dict(
            artifact_id=key,
            workspace_id=workspace,
            kind=row.kind,
            sha256=sha256(data).hexdigest(),
            size_bytes=len(data),
            mime_type="application/json",
            media_revision_id=None,
            timeline_revision_id=None,
            url=url,
            url_expires_at=utc(expires),
        )

    def evidence(self, workspace, key, *, session=None):
        row = self.repo.document(workspace, key, session=session)
        if row is not None and row.kind == "EVIDENCE":
            return row.body
        return None

    def content(self, workspace, key, expires, token):
        if expires < time.time() or not hmac.compare_digest(
            self.sign(workspace, key, expires).encode(), token.encode()
        ):
            raise EditError(404, "NOT_FOUND", "资源不存在")
        row = self.repo.document(workspace, key)
        if row is None:
            raise EditError(404, "NOT_FOUND", "资源不存在")
        return json_bytes(row.body)
