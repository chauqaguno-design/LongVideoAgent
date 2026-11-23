"""Read the verified deployment capabilities without making live model requests."""

import os

from fastapi import Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from dovideo.infrastructure.providers.flash_preflight import capabilities
from dovideo.infrastructure.persistence.edit_tables import jobs
from dovideo.infrastructure.persistence.processing_tables import runs as processing
from dovideo.infrastructure.persistence.workflow_tables import model_requests, runs as workflows
from dovideo.infrastructure.persistence.outbox_tables import outbox


def install_caps(app, require_user):
    @app.get("/v1/capabilities", dependencies=[Depends(require_user)])
    def get_capabilities():
        return capabilities(os.environ)

    @app.get("/v1/readiness")
    def readiness(user=Depends(require_user)):
        store = app.state.edit_sources
        scope = store.workspace(user)
        queues = {}
        try:
            with store.sessions() as db:
                for name, worker, table, owner in (
                    ("processing", app.state.processing, processing, processing.c.workspace),
                    ("workflow", app.state.workflow_worker, workflows, workflows.c.workspace_id),
                    ("render", app.state.edit_worker, jobs, jobs.c.workspace_id),
                    ("outbox", app.state.work_outbox, outbox, outbox.c.workspace),
                ):
                    counts = dict(
                        db.execute(
                            select(table.c.status, func.count())
                            .where(owner == scope)
                            .group_by(table.c.status)
                        ).all()
                    )
                    failures = counts.get("FAILED", 0) + counts.get("DEAD", 0)
                    completed = failures + sum(counts.get(s, 0) for s in ("SUCCEEDED", "PARTIAL", "SENT"))
                    queues[name] = dict(
                        online=worker.task is not None and not worker.task.done(),
                        counts=counts,
                        completed=completed,
                        failed=failures,
                        failure_rate=failures / completed if completed else None,
                    )
                uncertain = db.scalar(
                    select(func.count())
                    .select_from(model_requests.join(workflows))
                    .where(workflows.c.workspace_id == scope, model_requests.c.status == "UNCERTAIN")
                )
        except SQLAlchemyError:
            return JSONResponse({"ready": False, "dependency": "database"}, status_code=503)
        ready = all(q["online"] for q in queues.values())
        return JSONResponse(
            dict(ready=ready, queues=queues, uncertain_model_requests=uncertain),
            status_code=200 if ready else 503,
        )
