"""An inspection is resumable work scoped to the parent turn, source and frozen model budget."""

from cutsieve.application.edit_timeline import EditError
from cutsieve.application.workflow_identity import generation_key
from cutsieve.application.workflow_state import stable_id
from cutsieve.infrastructure.workflow_vision import review_interval


class WaitInspection(Exception):
    def __init__(self, job):
        self.outcome = dict(
            inspection=job,
            state=dict(status="WAITING_JOB", pending_job_ids=[job["job_id"]]),
            event="PROGRESS",
        )


def inspection(row, key, mid, span, goal, *, generation=None, underlying=(), preview=None):
    if mid is not None and mid not in row.checkpoint.spec.scope.media_revision_ids:
        raise EditError(404, "NOT_FOUND", "资源不存在")
    if mid is None and (
        not preview
        or preview
        not in (row.checkpoint.attempt.preview_artifact_id, row.checkpoint.attempt.export_artifact_id)
    ):
        raise EditError(404, "NOT_FOUND", "资源不存在")
    subject = f"{key}:{row.checkpoint.attempt.revision_id}:{preview}" if preview else key
    job_id = stable_id(row.id, generation_key(row.checkpoint, f"inspect:{row.checkpoint.attempt.turn}:{subject}"))
    existing = row.checkpoint.context.inspections.get(job_id)
    if existing:
        if existing["status"] == "FAILED":
            err = existing["error"]
            raise EditError(503 if err["retryable"] else 422, err["code"], err["message"])
        return existing["report"]
    job = dict(
        job_id=job_id,
        key=key,
        turn=row.checkpoint.attempt.turn,
        plan_generation=row.checkpoint.attempt.plan_generation,
        status="QUEUED",
        media=mid,
        range=span,
        goal=goal,
        generation=generation,
        underlying=list(underlying),
        preview=preview,
    )
    raise WaitInspection(job)


def pending_inspection(row):
    jobs = row.checkpoint.context.inspections
    return next(
        (
            jobs[key]
            for key in row.state["pending_job_ids"]
            if key in jobs
            and jobs[key]["turn"] == row.checkpoint.attempt.turn
            and jobs[key]["plan_generation"] == row.checkpoint.attempt.plan_generation
            and jobs[key]["status"] in ("QUEUED", "RUNNING")
        ),
        None,
    )


def run_inspection(repo, planner, render_worker, row, job):
    with repo.edits.write() as db:
        claim = repo.guard(db, row)
        checkpoint = claim.checkpoint
        checkpoint.context.inspections[job["job_id"]]["status"] = "RUNNING"
        repo.checkpoint_inspection(db, claim, checkpoint, job)
    try:
        output = None
        if job["media"]:
            file = repo.sources.source_path(row.workspace_id, job["media"])
        else:
            artifact = render_worker.artifact_record(row.workspace_id, job["preview"])
            if artifact.kind not in ("PREVIEW", "EXPORT") or artifact.revision_id != checkpoint.attempt.revision_id:
                raise EditError(409, "OUTPUT_REVISION_CHANGED", "成片与本次复核的时间线版本不一致")
            output = dict(timeline_revision_id=artifact.revision_id, output_artifact_id=artifact.id,
                          output_sha256=artifact.digest)
            file = render_worker.materialize(artifact)
        report = review_interval(
            repo,
            planner,
            row,
            job["job_id"] if output else job["key"],
            file,
            job["media"],
            job["range"],
            job["goal"],
            generation=job["generation"],
            underlying=job["underlying"],
            output=output,
        )
        status = "PARTIAL" if report["support"] == "UNCERTAIN" else "SUCCEEDED"
        outcome = dict(status=status, report=report)
    except EditError as exc:
        from cutsieve.application.workflow_state import public_error

        outcome = dict(status="FAILED", error=public_error(exc.code, str(exc), exc.status == 503))
    return dict(
        inspection=dict(job, **outcome),
        state=dict(status="RUNNING", pending_job_ids=[]),
        inspection_finished=job["job_id"],
    )
