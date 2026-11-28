"""Claim work, renew its lease and let the agent yield on render or user input."""

import asyncio
import hmac
import json
import logging
import time
from hashlib import sha256
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from cutsieve.application.agent_harness import AgentHarness
from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.application.workflow_state import utc

from .workflow_repository import StaleLease


log = logging.getLogger(__name__)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class WorkflowWorker:
    def __init__(self, repo, app, sign, *, transport=None):
        self.repo, self.sign = repo, sign
        self.harness = AgentHarness(repo, app.state.evidence_search, app.state.edit_worker, transport=transport)
        self.worker_id = uid()
        self.task = None

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
                    task = asyncio.create_task(asyncio.to_thread(self.run_claim, claim))
                    while not task.done():
                        done, _ = await asyncio.wait((task,), timeout=15)
                        if not done:
                            try:
                                await asyncio.to_thread(self.repo.renew, claim)
                            except StaleLease:
                                pass
                            except SQLAlchemyError:
                                log.warning("Workflow lease renewal unavailable")
                    await task
                    continue
            except SQLAlchemyError:
                log.exception("Workflow queue unavailable")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.45)
            except TimeoutError:
                pass

    def run_claim(self, claim):
        try:
            self.harness.run(claim)
        except StaleLease:
            self.repo.stopped(claim)
        except SQLAlchemyError:
            # A reclaimed lease reconciles the last committed checkpoint.
            raise
        except EditError as exc:
            self.fail(claim, exc.code, str(exc), exc.status >= 500)
        except HTTPException as exc:
            self.fail(claim, "RESOURCE_UNAVAILABLE", str(exc.detail), exc.status_code >= 500)
        except Exception:
            log.exception("Workflow agent failed for %s", claim.id)
            self.fail(claim, "AGENT_FAILED", "The agent could not finish this turn. Resume to retry.", True)

    def fail(self, claim, code, message, retryable):
        try:
            with self.repo.change(claim) as (db, row):
                self.repo.fail(db, row, code, message, retryable)
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
