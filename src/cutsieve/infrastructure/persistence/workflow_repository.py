"""Fenced workflow queue, checkpoints and events backed by the existing workflow tables."""

import json
import os
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from cutsieve.application.edit_timeline import EditError, missing, timestamp, uid
from cutsieve.application.workflow_checkpoint import Checkpoint, Clarification, RunSpec, Usage
from cutsieve.application.workflow_state import (
    DEADLINE_SECONDS, HARNESS_USAGE_FIELDS, RUNNABLE, TERMINAL, new_state, public_error, require_version,
    stable_id, sync_usage,
)
from .edit_repository import job_view, one, owned
from .edit_tables import jobs, projects
from .work_outbox import emit
from .workflow_clock import db_now
from .workflow_tables import documents, events, runs


TOOL_TEXT_LIMIT = 8_000


def encode_tool_text(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def tool_excerpt(value):
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, list):
        return [tool_excerpt(item) for item in value[:3]]
    if isinstance(value, dict):
        return {key: tool_excerpt(item) for key, item in value.items()}
    return value


def compact_tool_body(body, artifact_id):
    compact = dict(ok=body["ok"], summary="Full tool result saved as a workflow artifact.", artifact_id=artifact_id)
    for key, value in body.items():
        if key in compact or isinstance(value, list):
            continue
        excerpt = value[:3300] if isinstance(value, str) else value
        if key == "details" and isinstance(value, dict):
            excerpt = tool_excerpt(value)
            excerpt.update({f"{name}_count": len(items) for name, items in value.items() if isinstance(items, list)})
        if len(encode_tool_text(dict(compact, **{key: excerpt}))) <= TOOL_TEXT_LIMIT - 500:
            compact[key] = excerpt
    for key, values in body.items():
        if not isinstance(values, list):
            continue
        excerpt = {key + "_count": len(values), key: []}
        if len(encode_tool_text(compact | excerpt)) > TOOL_TEXT_LIMIT:
            continue
        compact.update(excerpt)
        for item in values:
            item = tool_excerpt(item)
            if len(encode_tool_text(dict(compact, **{key: [*compact[key], item]}))) > TOOL_TEXT_LIMIT - 500:
                break
            compact[key].append(item)
    return compact


class StaleLease(Exception):
    pass


class WorkflowRepository:
    def __init__(self, edits):
        self.edits = edits
        self.sources = edits.sources
        self.sessions = edits.sessions

    def _event(self, db, state, event_type, payload):
        sequence = state["last_event_sequence"] + 1
        event = dict(event_id=uid(), run_id=state["run_id"], sequence=sequence, type=event_type, payload=payload)
        db.execute(insert(events).values(id=event["event_id"], run_id=state["run_id"], seq=sequence, body=event))
        emit(db, event["event_id"], state["workspace_id"], state["run_id"], "workflow.event")
        state.update(last_event_sequence=sequence, last_event_id=event["event_id"])

    def save(self, db, row, *notices, release=False, delay=0, deadline=None):
        state, cp = row.state, row.checkpoint
        state["state_version"] += 1
        sync_usage(state, cp.usage)
        for event_type, payload in notices:
            self._event(db, state, event_type, payload)
        values = dict(state=state, checkpoint=cp, status=state["status"])
        if release:
            values.update(lease=0, ready_at=db_now(db) + delay)
        if deadline is not None:
            values["deadline"] = deadline
        db.execute(update(runs).where(runs.c.id == row.id).values(**values))

    def _scope(self, workspace, scope):
        project = scope["project_id"]
        self.sources.project(workspace, project)
        mids = list(dict.fromkeys(scope.get("media_revision_ids") or []))
        if mids:
            for mid in mids:
                media = self.sources.media(workspace, mid)
                if self.sources.asset(workspace, media["asset_id"])["project_id"] != project:
                    raise missing()
        else:
            cursor = None
            while True:
                page = self.sources.page(workspace, cursor, 100, project)
                mids.extend(a["media_revision_id"] for a in page["items"] if a["status"] == "AVAILABLE")
                cursor = page["next_cursor"]
                if not cursor:
                    break
        if len(mids) > 1000:
            raise EditError(422, "SCOPE_TOO_LARGE", "请选定不超过一千个媒体版本")
        return dict(project_id=project, media_revision_ids=mids)

    def create(self, workspace, body, *, search_request=None, parent_run_id=None, image_artifact_id=None):
        project = body["scope"]["project_id"]

        def save(db):
            scope = self._scope(workspace, body["scope"])
            if search_request:
                media_ids = scope["media_revision_ids"]
                if search_request["query_mode"] == "FIRST" and len(media_ids) > 1:
                    raise EditError(422, "SEARCH_SCOPE", "请选定单个视频后查找首次出现位置")
                span = search_request.get("range")
                if span and (
                    span["start_us"] >= span["end_us"]
                    or any(span["end_us"] > self.sources.media(workspace, mid)["duration_us"] for mid in media_ids)
                ):
                    raise EditError(422, "SEARCH_RANGE", "核查区间超出原片")
            requested_media = body["scope"].get("media_revision_ids", [])
            cp = Checkpoint(
                spec=RunSpec(
                    scope=scope, message=body["message"], parent_run_id=parent_run_id,
                    media_revision_id=requested_media[0] if len(requested_media) == 1 else None,
                    image_artifact_id=image_artifact_id,
                    search_request=dict(search_request, scope=scope) if search_request else None,
                ),
                usage=Usage(
                    max_steps=max(0, int(os.getenv("CUTSIEVE_HARNESS_MAX_STEPS", "15"))),
                    max_images=max(0, int(os.getenv("CUTSIEVE_HARNESS_MAX_IMAGES", "24"))),
                ),
            )
            if parent_run_id:
                parent = self.readable(db, workspace, parent_run_id)
                if parent.project_id != project:
                    raise missing()
                cp.edit_project_id = parent.checkpoint.edit_project_id
                cp.remember_media(parent.checkpoint.protected_media)
            if edit_id := body.get("edit_project_id"):
                one(db, owned(projects, workspace, edit_id).where(projects.c.project_id == project))
                cp.edit_project_id = edit_id
            self.sources.require_readable(db, workspace, cp.protected_media)

            now, run_id = db_now(db), uid()
            state = new_state(run_id, workspace, project, cp.usage, body.get("budget"))
            if target := body.get("supersedes"):
                previous = self.readable(db, workspace, target["run_id"], lock=True)
                if previous.project_id != project:
                    raise missing()
                require_version(previous.state, target["expected_state_version"])
                if previous.status in TERMINAL:
                    raise EditError(409, "TERMINAL_RUN", "只能替换仍在进行的任务")
                self._stop_pending(db, previous)
                previous.state.update(status="SUPERSEDED", superseded_by_run_id=run_id, awaiting_input=None)
                self.save(db, previous, ("STATUS", dict(status="SUPERSEDED", phase="AGENT")), release=True)

            db.execute(insert(runs).values(
                id=run_id, workspace_id=workspace, project_id=project, status="QUEUED", lease=0, fence=0,
                ready_at=now, deadline=now + DEADLINE_SECONDS, state=state, checkpoint=cp,
            ))
            self._event(db, state, "STATUS", dict(status="QUEUED", phase="AGENT"))
            db.execute(update(runs).where(runs.c.id == run_id).values(state=state))
            return state

        request = dict(body, parent_run_id=parent_run_id, image_artifact_id=image_artifact_id,
                       search_request=search_request)
        receipt = self.edits.with_receipt(workspace, project, "wf-create", request, save)[0]
        self.get(workspace, receipt["run_id"])
        return receipt

    def readable(self, db, workspace, run_id, *, lock=False):
        query = owned(runs, workspace, run_id)
        row = one(db, query.with_for_update() if lock else query)
        self.sources.require_readable(db, workspace, row.checkpoint.protected_media)
        return row

    def get(self, workspace, run_id):
        with self.sessions() as db:
            row = self.readable(db, workspace, run_id)
            if "usage" in row.state:
                return row.state
            # Older completed runs still expose their recorded usage.
            budget = row.state["budget_usage"]
            counts = {
                key: budget.get(key, 0) for key in ("model_calls", "tool_calls", "images", "input_tokens", "output_tokens")
            }
            usage = row.checkpoint.usage.model_copy(update=counts)
            if budget.get("currency") == "CNY":
                usage.estimated_cost_cny = (budget.get("committed_microunits") or 0) / 1_000_000
            return dict(row.state, usage=usage.model_dump(exclude=HARNESS_USAGE_FIELDS))

    def history(self, workspace, project, cursor, limit):
        def visible(row):
            try:
                return self.get(workspace, row.id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                return None

        page = self.edits.page(runs, workspace, visible, dict(project_id=project), cursor, limit)
        page["items"] = [state for state in page["items"] if state is not None]
        return page

    def control(self, workspace, run_id, action, body):
        self.get(workspace, run_id)

        def save(db):
            row = self.readable(db, workspace, run_id, lock=True)
            state, cp = row.state, row.checkpoint
            if "expected_state_version" in body:
                require_version(state, body["expected_state_version"])
            if action == "cancel":
                if row.status not in TERMINAL:
                    self._stop_pending(db, row)
                    state.update(status="CANCELLED", cancel_requested_at=timestamp(), awaiting_input=None)
                    self.save(db, row, ("RUN_CANCELLED", dict(status="CANCELLED")), release=True)
                return state
            if action == "budget":
                # Legacy clients can store a display limit. Harness limits are fixed at creation.
                state["budget_usage"]["limits"] = body["new_limits"]
                state["budget_usage"]["currency"] = body["new_limits"]["currency"]
                self.save(db, row, ("STATUS", dict(status=row.status, phase=state["phase"])))
                return state
            if row.status not in ("AWAITING_INPUT", "FAILED"):
                raise EditError(409, "INVALID_RUN_STATE", "此任务没有等待补充或重试")
            if row.status == "FAILED" and not state["error"]["retryable"]:
                raise EditError(409, "TERMINAL_RUN", "该任务无法重试，请开始新一轮对话")
            reply = body["message"].strip()
            if not reply:
                raise EditError(422, "INVALID_REQUEST", "请输入回复内容")
            now = db_now(db)
            notices = []
            if pending := cp.pending_ask_tool_call:
                cp.clarifications.append(Clarification(question=pending.question, reply=reply))
                payload = self.tool_content(
                    db, row, pending.tool_call_id, dict(ok=True, question=pending.question, reply=reply)
                )
                tool = next(item for item in cp.tool_steps if item.tool_call_id == pending.tool_call_id)
                step = cp.finish_tool(pending.tool_call_id, payload, "Received clarification", True,
                                      duration_ms=tool.duration_ms)
                notices.append(("TOOL_FINISHED", step))
                cp.pending_ask_tool_call = None
                deadline = row.deadline + (now - cp.paused_at)
            else:
                cp.retry_replies.append(reply)
                if row.status == "AWAITING_INPUT" and (question := (state["awaiting_input"] or {}).get("question")):
                    cp.clarifications.append(Clarification(question=question, reply=reply))
                deadline = now + DEADLINE_SECONDS
            cp.paused_at = None
            cp.lease_failures = 0
            state.update(status="QUEUED", phase="AGENT", error=None, awaiting_input=None)
            self.save(db, row, *notices, ("STATUS", dict(status="QUEUED", phase="AGENT")), release=True, deadline=deadline)
            return state

        return self.edits.with_receipt(workspace, run_id, "wf-" + action, body, save)[0]

    def _stop_pending(self, db, row):
        cp = row.checkpoint
        if pending := cp.pending_render_tool_call:
            key = stable_id(row.id, "cancel:" + pending.render_job_id)
            self.edits.cancel(row.workspace_id, pending.render_job_id, dict(idempotency_key=key), session=db)
        cp.pending_render_tool_call = None
        cp.pending_ask_tool_call = None
        for step in cp.tool_steps:
            if step.status == "RUNNING":
                step.status, step.ok = "CANCELLED", False
                step.error_code = "CANCELLED"
        row.state["pending_job_ids"] = []

    def fail(self, db, row, code, message, retryable=False):
        if not retryable:
            self._stop_pending(db, row)
        error = public_error(code, message, retryable)
        row.state.update(status="FAILED", error=error, awaiting_input=None)
        self.save(db, row, ("RUN_FAILED", dict(status="FAILED", error=error)), release=True)

    def claim(self, worker, lease_seconds=60):
        with self.edits.write() as db:
            now = db_now(db)
            row = db.execute(
                select(runs).where(runs.c.status.in_(RUNNABLE), runs.c.lease <= now, runs.c.ready_at <= now)
                .order_by(runs.c.ready_at, runs.c.id).limit(1).with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return None
            if row.state["cancel_requested_at"]:
                self._stop_pending(db, row)
                row.state.update(status="CANCELLED", awaiting_input=None)
                self.save(db, row, ("RUN_CANCELLED", dict(status="CANCELLED")), release=True)
                return None
            try:
                self.sources.require_readable(db, row.workspace_id, row.checkpoint.protected_media)
            except HTTPException:
                self.fail(db, row, "SOURCE_REVOKED", "源素材已不可用")
                return None
            if row.deadline <= now:
                self.fail(db, row, "DEADLINE_EXCEEDED", "任务已到达时间上限")
                return None
            cp = row.checkpoint
            if row.lease:
                cp.lease_failures += 1
            if cp.lease_failures > 3:
                self.fail(db, row, "LEASE_RETRY_EXHAUSTED", "连续接管后任务仍未完成", True)
                return None
            if row.status == "QUEUED" or row.state["phase"] != "AGENT":
                row.state.update(status="RUNNING", phase="AGENT")
                self._event(db, row.state, "STATUS", dict(status="RUNNING", phase="AGENT"))
                row.state["state_version"] += 1
            db.execute(update(runs).where(runs.c.id == row.id).values(
                lease=min(row.deadline, now + lease_seconds), fence=row.fence + 1,
                status=row.state["status"], state=row.state, checkpoint=cp,
            ))
            return one(db, select(runs).where(runs.c.id == row.id))

    def guard(self, db, claim, *, lock=True):
        query = select(runs).where(runs.c.id == claim.id)
        row = one(db, query.with_for_update() if lock else query)
        now = db_now(db)
        if row.fence != claim.fence or row.status not in RUNNABLE or row.lease <= now or row.deadline <= now:
            raise StaleLease()
        try:
            self.sources.require_readable(db, row.workspace_id, row.checkpoint.protected_media)
        except HTTPException as exc:
            raise StaleLease() from exc
        return row

    @contextmanager
    def change(self, claim):
        with self.edits.write() as db:
            yield db, self.guard(db, claim)

    def remaining(self, claim):
        with self.sessions() as db:
            return max(0, claim.deadline - db_now(db))

    def renew(self, claim):
        with self.change(claim) as (db, row):
            db.execute(update(runs).where(runs.c.id == row.id).values(lease=min(row.deadline, db_now(db) + 60)))

    def stopped(self, claim):
        with self.edits.write() as db:
            row = one(db, select(runs).where(runs.c.id == claim.id).with_for_update())
            if row.fence == claim.fence and row.status in RUNNABLE and row.lease:
                if row.deadline <= db_now(db):
                    self.fail(db, row, "DEADLINE_EXCEEDED", "任务已到达时间上限")
                else:
                    try:
                        self.sources.require_readable(db, row.workspace_id, row.checkpoint.protected_media)
                    except HTTPException:
                        self.fail(db, row, "SOURCE_REVOKED", "源素材已不可用")

    def defer_render(self, db, row):
        db.execute(update(runs).where(runs.c.id == row.id).values(lease=0, ready_at=db_now(db) + 1))

    def tool_content(self, db, row, call_id, body):
        content = encode_tool_text(body)
        if len(content) > TOOL_TEXT_LIMIT:
            key = self.output(db, row, "tool:" + call_id, body, "TOOL_RESULT")
            content = encode_tool_text(compact_tool_body(body, key))
        return content

    def complete(self, db, row, result):
        key = self.output(db, row, "result", result, "QUERY_RESULT")
        row.state.update(status="SUCCEEDED", phase="DONE", result_artifact_id=key, pending_job_ids=[])
        self.save(db, row, ("RUN_COMPLETED", dict(status="SUCCEEDED", result_artifact_id=key)), release=True)

    @staticmethod
    def output(db, row, label, body, kind):
        key = stable_id(row.id, "agent:" + label)
        db.execute(insert(documents).values(id=key, run_id=row.id, kind=kind, body=body))
        return key

    def render_job(self, db, row, job_id):
        if job_id not in row.checkpoint.render_job_ids:
            raise missing()
        return job_view(one(db, owned(jobs, row.workspace_id, job_id)))

    def event_cursor(self, workspace, run_id, cursor):
        with self.sessions() as db:
            self.readable(db, workspace, run_id)
            if not cursor:
                return 0
            index = db.scalar(select(events.c.seq).where(events.c.run_id == run_id, events.c.id == cursor))
        if index is None:
            raise EditError(409, "CURSOR_EXPIRED", "事件游标已失效，请重新读取任务快照")
        return index

    def page(self, workspace, run_id, cursor, limit):
        index = int(cursor) if cursor and cursor.isascii() and cursor.isdigit() and len(cursor) < 17 else -1
        if cursor and index < 0:
            raise EditError(422, "INVALID_CURSOR", "无效分页游标")
        with self.sessions() as db:
            self.readable(db, workspace, run_id)
            rows = db.execute(
                select(events).where(events.c.run_id == run_id, events.c.seq > index).order_by(events.c.seq).limit(limit + 1)
            ).all()
        return dict(
            items=[r.body for r in rows[:limit]],
            next_cursor=str(rows[limit - 1].seq) if len(rows) > limit else None,
        )

    def document(self, workspace, key, *, session=None):
        if session is None:
            with self.sessions() as db:
                return self.document(workspace, key, session=db)
        row = session.execute(
            select(documents, runs.c.checkpoint).join(runs, documents.c.run_id == runs.c.id)
            .where(runs.c.workspace_id == workspace, documents.c.id == key)
        ).first()
        if row:
            self.sources.require_readable(session, workspace, row.checkpoint.protected_media)
        return row
