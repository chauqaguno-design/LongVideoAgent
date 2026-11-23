"""A checkpoint, its event cursor, and optional tool receipt commit atomically."""

import time
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import and_, insert, select, update

from dovideo.application.edit_timeline import EditError, missing, timestamp, uid
from dovideo.application.workflow_routing import candidate_reference
from dovideo.application.workflow_state import (
    DEFAULT_BUDGET,
    RUNNABLE,
    TERMINAL,
    NeedsInput,
    budget_change,
    new_state,
    public_error,
    require_version,
    stable_id,
    task_label,
)
from .edit_repository import digest, one, owned, project_view
from .edit_tables import projects, revisions
from .workflow_tables import calls, documents, events, model_requests, runs, tasks
from .workflow_team import claim_stage, create_team, finish_stage, stage_row, write_task
from .work_outbox import emit
from .workflow_clock import db_now


class StaleLease(Exception):
    pass


class WorkflowRepository:
    def __init__(self, edits):
        self.edits = edits
        self.sources = edits.sources
        self.sessions = edits.sessions

    def _event(self, db, state, event_type, payload):
        sequence = state["last_event_sequence"] + 1
        event = dict(
            event_id=uid(),
            run_id=state["run_id"],
            sequence=sequence,
            type=event_type,
            created_at=timestamp(),
            payload=payload,
        )
        db.execute(
            insert(events).values(id=event["event_id"], run_id=state["run_id"], seq=sequence, body=event)
        )
        emit(db, event["event_id"], state["workspace_id"], state["run_id"], "workflow.event")
        state.update(last_event_sequence=sequence, last_event_id=event["event_id"])

    def _update(self, db, row, state, checkpoint, event_type, payload=None, *, ready=0, deadline=None):
        state["state_version"] += 1
        self._event(db, state, event_type, payload or dict(phase=state["phase"], status=state["status"]))
        db.execute(
            update(runs)
            .where(runs.c.id == row.id)
            .values(
                state=state,
                checkpoint=checkpoint,
                status=state["status"],
                ready_at=ready,
                lease=0,
                deadline=row.deadline if deadline is None else deadline,
            )
        )

    def trace(self, db, row, state, activity, *, origin=None, **metadata):
        """Publish bounded execution metadata without copying prompts, tool arguments or source dialogue."""
        origin = row if origin is None else origin
        phase = origin.state["phase"]
        payload = dict(activity=activity, phase=phase, fencing_token=origin.fence, **metadata)
        if origin.checkpoint["team"]:
            payload["task_id"] = stable_id(origin.id, task_label(phase, origin.checkpoint["turn"]))
        self._event(db, state, "PROGRESS", payload)
        state["state_version"] += 1
        db.execute(update(runs).where(runs.c.id == row.id).values(state=state))

    def _scope(self, workspace, scope):
        project = scope["project_id"]
        self.sources.project(workspace, project)
        if scope.get("media_revision_ids"):
            mids = list(dict.fromkeys(scope["media_revision_ids"]))
        else:
            cursor, mids = None, []
            while True:
                page = self.sources.page(workspace, cursor, 100, project)
                mids.extend(a["media_revision_id"] for a in page["items"] if a["status"] == "AVAILABLE")
                cursor = page["next_cursor"]
                if not cursor:
                    break
        if len(mids) > 1000:
            raise EditError(422, "SCOPE_TOO_LARGE", "请选定不超过一千个媒体版本")
        for mid in mids:
            media = self.sources.media(workspace, mid)
            if self.sources.asset(workspace, media["asset_id"])["project_id"] != project:
                raise missing()
        return {"project_id": project, "media_revision_ids": mids}

    def create(self, workspace, body, *, team=False, search_request=None, conversation=None):
        project = body["scope"]["project_id"]

        def save(db):
            scope = self._scope(workspace, body["scope"])
            run_id, now = uid(), db_now(db)
            state = new_state(run_id, workspace, project, body.get("budget", dict(DEFAULT_BUDGET)))
            cp = dict(
                intent=body["intent"], message=body["message"], scope=scope, turn=0, team=team, created=now
            )
            if conversation is not None:
                if parent := conversation.get("parent_run_id"):
                    previous = self.readable(db, workspace, parent)
                    if previous.project_id != project or previous.checkpoint["scope"] != scope:
                        raise missing()
                cp["conversation"] = conversation
            if search_request is not None:
                cp["search_request"] = dict(search_request, scope=scope)
                media_ids = scope["media_revision_ids"]
                if search_request["query_mode"] == "FIRST" and len(media_ids) > 1:
                    raise EditError(422, "SEARCH_SCOPE", "请选定单个视频后查找首次出现位置")
                span = search_request.get("range")
                if span and (
                    span["start_us"] >= span["end_us"]
                    or any(
                        span["end_us"] > self.sources.media(workspace, mid)["duration_us"]
                        for mid in media_ids
                    )
                ):
                    raise EditError(422, "SEARCH_RANGE", "核查区间超出原片")
            if body.get("edit_project_id"):
                edit = self.edits.get(
                    projects, workspace, body["edit_project_id"], project_view, project_id=project
                )
                cp.update(
                    edit_id=edit["edit_project_id"],
                    base=edit["head_revision"],
                    base_id=edit["head_revision_id"],
                )
                timeline = self.edits.get(revisions, workspace, cp["base_id"], lambda r: r.snapshot)
                if any(
                    clip["media_revision_id"] not in scope["media_revision_ids"] for clip in timeline["clips"]
                ):
                    raise EditError(422, "EDIT_SCOPE", "所选素材范围需要包含现有时间线的全部原片")
            if body.get("supersedes"):
                target = body["supersedes"]
                previous = one(
                    db,
                    owned(runs, workspace, target["run_id"])
                    .where(runs.c.project_id == project)
                    .with_for_update(),
                )
                self.sources.require_readable(
                    db, workspace, previous.checkpoint["scope"]["media_revision_ids"]
                )
                before = deepcopy(previous.state)
                require_version(before, target["expected_state_version"])
                if previous.status in TERMINAL:
                    raise EditError(409, "TERMINAL_RUN", "只能替换仍在进行的任务")
                before.update(
                    status="SUPERSEDED", superseded_by_run_id=run_id, pending_job_ids=[], awaiting_input=None
                )
                finish_stage(db, previous, before["phase"], before["phase"], "SUPERSEDED", [])
                self._update(db, previous, before, previous.checkpoint, "STATUS")
            db.execute(
                insert(runs).values(
                    id=run_id,
                    workspace_id=workspace,
                    project_id=project,
                    status="QUEUED",
                    lease=0,
                    fence=0,
                    ready_at=now,
                    deadline=now + state["budget_usage"]["limits"]["deadline_seconds"],
                    state=state,
                    checkpoint=cp,
                )
            )
            if team:
                create_team(db, run_id, workspace, body["intent"])
            self._event(db, state, "STATUS", dict(status="QUEUED", phase="PLAN"))
            db.execute(update(runs).where(runs.c.id == run_id).values(state=state))
            return state

        receipt_body = dict(body, search_request=search_request) if search_request is not None else body
        if conversation is not None:
            receipt_body = dict(body, parent_run_id=conversation.get("parent_run_id"))
        kind = "chat-create" if conversation is not None else "wf-create"
        receipt = self.edits.with_receipt(workspace, project, kind, receipt_body, save)[0]
        self.get(workspace, receipt["run_id"])
        return receipt

    def readable(self, db, workspace, run_id, *, lock=False):
        query = owned(runs, workspace, run_id)
        row = one(db, query.with_for_update() if lock else query)
        self.sources.require_readable(db, workspace, row.checkpoint["scope"]["media_revision_ids"])
        return row

    def get(self, workspace, run_id):
        with self.sessions() as db:
            return self.readable(db, workspace, run_id).state

    def history(self, workspace, project, cursor, limit):
        def visible(row):
            try:
                return self.get(workspace, row.id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                return None

        page = self.edits.page(runs, workspace, visible, dict(project_id=project), cursor, limit)
        page["items"] = [s for s in page["items"] if s is not None]
        return page

    def control(self, workspace, run_id, action, body):
        self.get(workspace, run_id)

        def save(db):
            row = self.readable(db, workspace, run_id, lock=True)
            state, cp = deepcopy(row.state), deepcopy(row.checkpoint)
            deadline, kind = row.deadline, "STATUS"
            if "expected_state_version" in body:
                require_version(state, body["expected_state_version"])
            if action == "cancel" and (row.status in TERMINAL or state["cancel_requested_at"]):
                return state
            if state["cancel_requested_at"]:
                raise EditError(409, "CANCELLING", "任务正在取消")
            if row.status in TERMINAL and not (
                action == "resume" and row.status == "FAILED" and state["error"]["retryable"]
            ):
                raise EditError(409, "TERMINAL_RUN", "该任务已经结束")
            if action == "cancel":
                state["cancel_requested_at"] = timestamp()
                pending = db.scalar(
                    select(model_requests.c.id)
                    .where(model_requests.c.run_id == run_id, model_requests.c.status == "PENDING")
                    .limit(1)
                )
                inspecting = any(job["status"] == "RUNNING" for job in cp.get("inspections", {}).values())
                if (pending or inspecting) and row.lease > db_now(db):
                    self.trace(db, row, state, "cancel.requested")
                else:
                    self.cancelled(db, row, state)
                return state
            elif action == "budget":
                budget_change(state, body["new_limits"])
                deadline = cp["created"] + body["new_limits"]["deadline_seconds"]
            else:
                if row.status not in ("AWAITING_INPUT", "FAILED"):
                    raise EditError(409, "INVALID_RUN_STATE", "此任务没有等待补充或重试")
                awaiting = state["awaiting_input"]
                if awaiting and awaiting["kind"] != "BUDGET":
                    if "conversation" in cp:
                        cp.setdefault("clarifications", []).append(
                            dict(question=awaiting["question"], reply=body["message"])
                        )
                        choices = cp.get("conversation_candidates", [])
                        if choices:
                            number = candidate_reference(body["message"])
                            if number == 0 and len(choices) == 1:
                                number = 1
                            if number is not None:
                                if not 1 <= number <= len(choices):
                                    raise EditError(
                                        422, "INVALID_CANDIDATE", "请选择本轮清单中的一个候选编号"
                                    )
                                cp["selected_candidate"] = choices[number - 1]
                    if cp["team"]:
                        if cp["turn"] >= 3:
                            raise EditError(409, "REPLAN_LIMIT", "该团队运行已达到调整次数上限，请创建新任务")
                        finish_stage(db, row, state["phase"], state["phase"], "SUPERSEDED", [])
                        create_team(db, row.id, workspace, cp["intent"], cp["turn"] + 1)
                    cp.update(turn=cp["turn"] + 1, clarification=body["message"])
                    if cp.get("revision_id"):
                        current = one(db, owned(revisions, workspace, cp["revision_id"]))
                        cp.update(base=current.number, base_id=current.id)
                    for key in (
                        "dialogs",
                        "model_round",
                        "format_repaired",
                        "native_call_ids",
                        "revision_id",
                        "render_job_id",
                        "preview_artifact_id",
                        "qa_id",
                        "export_job_id",
                        "export_artifact_id",
                        "export_qa_id",
                    ):
                        cp.pop(key, None)
                    state.update(phase="PLAN", pending_job_ids=[], result_artifact_id=None, evidence_ids=[])
                state.update(status="QUEUED", error=None, awaiting_input=None)
            payload = dict(phase=state["phase"], status=state["status"], action=action)
            if action == "budget":
                payload.update(reason=body["reason"], limits=body["new_limits"])
            self._update(db, row, state, cp, kind, payload, deadline=deadline)
            return state

        return self.edits.with_receipt(workspace, run_id, "wf-" + action, body, save)[0]

    def cancelled(self, db, row, state):
        state.update(status="CANCELLED", awaiting_input=None, pending_job_ids=[])
        finish_stage(db, row, state["phase"], state["phase"], "CANCELLED", [])
        self._update(db, row, state, row.checkpoint, "CANCELLED")

    def stopped(self, claim):
        with self.edits.write() as db:
            row = db.execute(select(runs).where(runs.c.id == claim.id).with_for_update()).one()
            if row.fence == claim.fence and row.state["cancel_requested_at"] and row.status not in TERMINAL:
                self.cancelled(db, row, deepcopy(row.state))

    def claim(self, worker, lease_seconds=60):
        with self.edits.write() as db:
            now = db_now(db)
            ready = and_(runs.c.status.in_(RUNNABLE), runs.c.lease <= now, runs.c.ready_at <= now)
            row = db.execute(
                select(runs)
                .where(ready)
                .order_by(runs.c.ready_at, runs.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if not row:
                return None
            if row.state["cancel_requested_at"]:
                self.cancelled(db, row, deepcopy(row.state))
                return None
            try:
                self.sources.require_readable(
                    db, row.workspace_id, row.checkpoint["scope"]["media_revision_ids"]
                )
            except HTTPException:
                state = deepcopy(row.state)
                state.update(
                    status="FAILED",
                    pending_job_ids=[],
                    error=public_error("SOURCE_REVOKED", "源素材已不可用"),
                )
                finish_stage(db, row, state["phase"], state["phase"], "FAILED", [])
                self._update(db, row, state, row.checkpoint, "FAILED")
                return None
            if row.deadline <= now:
                state = deepcopy(row.state)
                state.update(status="FAILED", error=public_error("DEADLINE_EXCEEDED", "任务已到达时间上限"))
                finish_stage(db, row, state["phase"], state["phase"], "FAILED", [])
                self._update(db, row, state, row.checkpoint, "FAILED")
                return None
            checkpoint = deepcopy(row.checkpoint)
            checkpoint["lease_owner"] = worker
            if row.lease:
                checkpoint["lease_failures"] = checkpoint.get("lease_failures", 0) + 1
            if checkpoint.get("lease_failures", 0) > 3:
                state = deepcopy(row.state)
                state.update(
                    status="FAILED", error=public_error("LEASE_RETRY_EXHAUSTED", "连续三次接管后仍未完成")
                )
                finish_stage(db, row, state["phase"], state["phase"], "FAILED", [])
                self._update(db, row, state, checkpoint, "FAILED")
                return None
            fence = row.fence + 1
            state = deepcopy(row.state)
            if row.status == "QUEUED":
                state["status"] = "RUNNING"
                state["state_version"] += 1
                self._event(db, state, "STATUS", dict(status="RUNNING", phase=state["phase"]))
            expiry = min(row.deadline, now + lease_seconds)
            db.execute(
                update(runs)
                .where(runs.c.id == row.id)
                .values(lease=expiry, fence=fence, status=state["status"], state=state, checkpoint=checkpoint)
            )
            if row.checkpoint["team"]:
                try:
                    claim_stage(db, row, worker, expiry, fence)
                except EditError as exc:
                    state.update(status="FAILED", pending_job_ids=[], error=public_error(exc.code, str(exc)))
                    finish_stage(db, row, state["phase"], state["phase"], "FAILED", [])
                    self._update(db, row, state, checkpoint, "FAILED")
                    return None
            return one(db, select(runs).where(runs.c.id == row.id))

    def guard(self, db, claim):
        row = db.execute(select(runs).where(runs.c.id == claim.id).with_for_update()).first()
        if (
            not row
            or row.fence != claim.fence
            or row.checkpoint.get("lease_owner") != claim.checkpoint.get("lease_owner")
            or row.status not in RUNNABLE
            or row.state["cancel_requested_at"]
            or min(row.lease, row.deadline) <= db_now(db)
        ):
            raise StaleLease()
        try:
            self.sources.require_readable(db, row.workspace_id, row.checkpoint["scope"]["media_revision_ids"])
        except HTTPException as exc:
            raise StaleLease() from exc
        return row

    def remaining(self, claim):
        with self.sessions() as db:
            return max(0, claim.deadline - db_now(db))

    def renew(self, claim):
        with self.edits.write() as db:
            row = self.guard(db, claim)
            expiry = min(row.deadline, db_now(db) + 60)
            db.execute(update(runs).where(runs.c.id == row.id).values(lease=expiry))
            if row.checkpoint["team"]:
                from dovideo.application.workflow_state import utc

                stage = stage_row(db, row, row.state["phase"])
                write_task(db, stage, lease_until=utc(expiry))

    def writer_guard(self, db, claim, phase):
        row = self.guard(db, claim)
        if phase not in ("COMMIT", "RENDER") or row.state["phase"] != phase:
            raise EditError(403, "WRITER_REQUIRED", "当前阶段没有时间线或渲染写权限")
        if row.checkpoint["team"]:
            card = stage_row(db, row, phase).body
            if (
                card["role"] != "COORDINATOR"
                or card["claimed_by"] != row.checkpoint.get("lease_owner")
                or card["fencing_token"] != claim.fence
                or card["status"] != "RUNNING"
            ):
                raise EditError(403, "WRITER_REQUIRED", "需要当前协调器的写租约")

    def output(self, db, row, label, body, kind="REPORT"):
        key = stable_id(row.id, f"{label}:{row.checkpoint['turn']}")
        if not db.execute(select(documents.c.id).where(documents.c.id == key)).scalar():
            db.execute(insert(documents).values(id=key, run_id=row.id, kind=kind, body=body))
        return key

    def checkpoint_inspection(self, db, row, checkpoint, job):
        db.execute(update(runs).where(runs.c.id == row.id).values(checkpoint=checkpoint))
        self.trace(db, row, deepcopy(row.state), "inspection.started", job_id=job["job_id"])

    def advance(self, claim, **outcome):
        with self.edits.write() as db:
            row = self.guard(db, claim)
            state, cp = deepcopy(row.state), deepcopy(row.checkpoint)
            if outcome.get("sleep"):
                db.execute(update(runs).where(runs.c.id == row.id).values(lease=0, ready_at=db_now(db) + 1))
                return
            cp.update(outcome.get("checkpoint", {}))
            if job_id := outcome.get("inspection_finished"):
                emit(db, uid(), row.workspace_id, job_id, "inspection.finished")
            cp.pop("lease_failures", None)
            state.update(outcome.get("state", {}))
            doc_ids = []
            for label, body in outcome.get("docs", {}).items():
                key = self.output(db, row, label, body, "QUERY_RESULT" if label == "result" else "REPORT")
                cp[label + "_id"] = key
                doc_ids.append(key)
                if label == "result":
                    state["result_artifact_id"] = key
            if cp["team"] and not row.checkpoint["team"]:
                create_team(db, row.id, row.workspace_id, cp["intent"], cp["turn"])
            elif cp["team"]:
                finish_stage(db, row, row.state["phase"], state["phase"], state["status"], doc_ids)
            payload = dict(phase=state["phase"], status=state["status"], **(outcome.get("payload") or {}))
            self._update(db, row, state, cp, outcome.get("event", "PROGRESS"), payload)

    def invoke(self, claim, key, name, arguments, execute):
        """Persist intent before execution. Effect adapters reuse this stable key on recovery."""
        predicate = and_(calls.c.run_id == claim.id, calls.c.call_key == key)
        with self.edits.write() as db:
            row = self.guard(db, claim)
            old = db.execute(select(calls).where(predicate)).first()
            if old and (old.name != name or old.input_digest != digest(arguments)):
                raise EditError(409, "TOOL_ARGUMENT_CONFLICT", "工具重试参数与已登记请求不符")
            if old and old.response is not None:
                return old.response
            state = deepcopy(row.state)
            usage = state["budget_usage"]
            if usage["tool_calls"] >= usage["limits"]["max_tool_calls"]:
                raise NeedsInput("工具调用次数已达到上限。请调整预算后继续。", kind="BUDGET")
            usage["tool_calls"] += 1
            attempt = old.attempts + 1 if old else 1
            self.trace(db, row, state, "tool.started", tool=name, call_key=key, attempt=attempt)
            if old:
                db.execute(update(calls).where(predicate).values(attempts=old.attempts + 1))
            else:
                db.execute(
                    insert(calls).values(
                        run_id=row.id, call_key=key, name=name, attempts=1, input_digest=digest(arguments)
                    )
                )
        start = time.monotonic()
        result = execute()
        with self.edits.write() as db:
            row = self.guard(db, claim)
            db.execute(update(calls).where(predicate).values(response=result))
            metadata = {
                k: result[k] for k in ("render_job_id", "revision_id", "edit_project_id") if k in result
            }
            self.trace(
                db,
                row,
                deepcopy(row.state),
                "tool.finished",
                tool=name,
                call_key=key,
                attempt=attempt,
                latency_ms=int((time.monotonic() - start) * 1000),
                **metadata,
            )
        return result

    def event_cursor(self, workspace, run_id, cursor):
        with self.sessions() as db:
            self.readable(db, workspace, run_id)
            if not cursor:
                return 0
            n = db.execute(
                select(events.c.seq).where(events.c.run_id == run_id, events.c.id == cursor)
            ).scalar()
        if n is None:
            raise EditError(409, "CURSOR_EXPIRED", "事件游标已失效，请重新读取任务快照")
        return n

    def page(self, workspace, run_id, table, cursor, limit):
        order = table.c.position if table is tasks else table.c.seq
        index = int(cursor) if cursor and cursor.isascii() and cursor.isdigit() and len(cursor) < 17 else -1
        if cursor and index < 0:
            raise EditError(422, "INVALID_CURSOR", "无效分页游标")
        with self.sessions() as db:
            self.readable(db, workspace, run_id)
            rows = db.execute(
                select(table).where(table.c.run_id == run_id, order > index).order_by(order).limit(limit + 1)
            ).all()
        return dict(
            items=[r.body for r in rows[:limit]],
            next_cursor=str(rows[limit - 1]._mapping[order]) if len(rows) > limit else None,
        )

    def document(self, workspace, key, *, session=None):
        if session is None:
            with self.sessions() as db:
                return self.document(workspace, key, session=db)
        row = session.execute(
            select(documents, runs.c.checkpoint)
            .join(runs, documents.c.run_id == runs.c.id)
            .where(runs.c.workspace_id == workspace, documents.c.id == key)
        ).first()
        if row:
            self.sources.require_readable(session, workspace, row.checkpoint["scope"]["media_revision_ids"])
        return row
