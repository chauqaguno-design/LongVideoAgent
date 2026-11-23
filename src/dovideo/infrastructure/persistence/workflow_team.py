"""Finite task graph. Claims, completion and mailbox delivery use the owning run transaction."""

from graphlib import CycleError, TopologicalSorter
from sqlalchemy import insert, select, update
from dovideo.application.edit_timeline import EditError
from dovideo.application.workflow_state import STAGES, stable_id, task_label, utc
from dovideo.application.edit_timeline import timestamp, uid
from .workflow_tables import attempts, deliveries, mail, tasks
from .work_outbox import emit
from .workflow_clock import db_now


def board(run_id, workspace, intent, turn=0):
    stages = STAGES if intent == "EDIT" else (STAGES[0], STAGES[1], STAGES[-1])
    parent = []
    entries = []
    for index, (stage, role) in enumerate(stages):
        label = task_label(stage, turn)
        key = stable_id(run_id, label)
        body = dict(
            task_id=key,
            run_id=run_id,
            workspace_id=workspace,
            role=role,
            status="BLOCKED" if parent else "READY",
            depends_on=parent,
            claimed_by=None,
            lease_until=None,
            fencing_token=0,
            attempt=0,
            input_artifact_ids=[],
            output_artifact_ids=[],
        )
        entries.append(
            dict(id=key, run_id=run_id, stage=label, position=turn * len(STAGES) + index, body=body)
        )
        parent = [key]
    return entries


def install_board(db, run_id, workspace, entries):
    roles = dict(STAGES)
    ids = {entry["id"] for entry in entries}
    graph = {}
    if len(ids) != len(entries) or len(ids) > len(STAGES):
        raise EditError(422, "INVALID_TASK_GRAPH", "任务图包含重复或过多节点")
    for entry in entries:
        task = entry["body"]
        role = roles.get(entry["stage"].split(":")[0])
        if (
            not role
            or task["role"] != role
            or task["run_id"] != run_id
            or entry["run_id"] != run_id
            or task["workspace_id"] != workspace
            or task["task_id"] != entry["id"]
            or not set(task["depends_on"]).issubset(ids)
        ):
            raise EditError(422, "INVALID_TASK_GRAPH", "任务角色、来源或依赖不属于当前任务图")
        graph[entry["id"]] = task["depends_on"]
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise EditError(422, "INVALID_TASK_GRAPH", "任务依赖形成了环") from exc
    db.execute(insert(tasks), entries)


def create_team(db, run_id, workspace, intent, turn=0):
    install_board(db, run_id, workspace, board(run_id, workspace, intent, turn))


def write_task(db, entry, **changes):
    db.execute(update(tasks).where(tasks.c.id == entry.id).values(body=dict(entry.body, **changes)))


def stage_row(db, row, phase):
    turn = row.checkpoint["turn"]
    label = task_label(phase, turn)
    return db.execute(select(tasks).where(tasks.c.run_id == row.id, tasks.c.stage == label)).first()


def notify(db, row, sender, recipient, kind, ids, summary):
    last = (
        db.execute(
            select(mail.c.seq).where(mail.c.run_id == row.id).order_by(mail.c.seq.desc()).limit(1)
        ).scalar()
        or 0
    )
    message = dict(
        message_id=uid(),
        run_id=row.id,
        sender_task_id=sender,
        recipient_task_id=recipient,
        type=kind,
        sequence=last + 1,
        artifact_ids=ids,
        summary=summary,
        created_at=timestamp(),
    )
    db.execute(insert(mail).values(id=message["message_id"], run_id=row.id, seq=last + 1, body=message))
    emit(db, message["message_id"], row.workspace_id, row.id, "team.mail")
    db.execute(
        insert(deliveries).values(
            id=message["message_id"], run_id=row.id, recipient=recipient, expires=row.deadline
        )
    )


def claim_stage(db, row, owner, expiry, token):
    entry = stage_row(db, row, row.state["phase"])
    if entry is None:
        raise EditError(409, "TASK_DEPENDENCY", "当前阶段缺少已冻结的任务卡")
    if entry is not None:
        parents = (
            db.execute(
                select(tasks.c.body).where(tasks.c.run_id == row.id, tasks.c.id.in_(entry.body["depends_on"]))
            )
            .scalars()
            .all()
        )
        if len(parents) != len(entry.body["depends_on"]) or any(p["status"] != "SUCCEEDED" for p in parents):
            raise EditError(409, "TASK_DEPENDENCY", "依赖任务尚未完成")
        now = db_now(db)
        db.execute(
            update(attempts)
            .where(attempts.c.task_id == entry.id, attempts.c.outcome.is_(None))
            .values(end_at=now, outcome="EXPIRED")
        )
        db.execute(
            insert(attempts).values(
                id=uid(), run_id=row.id, task_id=entry.id, owner=owner, fence=token, start_at=now
            )
        )
        db.execute(
            update(deliveries)
            .where(
                deliveries.c.run_id == row.id,
                deliveries.c.recipient == entry.id,
                deliveries.c.accepted.is_(None),
                deliveries.c.expires > now,
            )
            .values(accepted=now)
        )
        write_task(
            db,
            entry,
            status="RUNNING",
            claimed_by=owner,
            lease_until=utc(expiry),
            fencing_token=token,
            attempt=entry.body["attempt"] + 1,
        )


def finish_stage(db, row, old_phase, next_phase, status, artifact_ids):
    entry = stage_row(db, row, old_phase)
    if entry is None:
        return
    db.execute(
        update(attempts)
        .where(attempts.c.task_id == entry.id, attempts.c.outcome.is_(None))
        .values(end_at=db_now(db), outcome=status)
    )
    if status in ("CANCELLED", "SUPERSEDED", "FAILED"):
        for task in db.execute(select(tasks).where(tasks.c.run_id == row.id)).all():
            if task.body["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                write_task(
                    db,
                    task,
                    status="FAILED" if status == "FAILED" and task.id == entry.id else "CANCELLED",
                    claimed_by=None,
                    lease_until=None,
                )
                if task.id != entry.id:
                    notify(db, row, stage_row(db, row, "PLAN").id, task.id, "CANCEL", [], "父任务已停止")
        return
    if status == "AWAITING_INPUT" or (old_phase == next_phase and status != "SUCCEEDED"):
        write_task(
            db,
            entry,
            status="BLOCKED" if status == "AWAITING_INPUT" else "READY",
            claimed_by=None,
            lease_until=None,
        )
        return
    write_task(
        db, entry, status="SUCCEEDED", claimed_by=None, lease_until=None, output_artifact_ids=artifact_ids
    )
    target = stage_row(db, row, next_phase)
    if target is not None and target.id != entry.id:
        write_task(
            db, target, status="READY", input_artifact_ids=artifact_ids, claimed_by=None, lease_until=None
        )
        notify(db, row, entry.id, target.id, "DEPENDENCY_READY", artifact_ids, old_phase + " 已完成")
