"""Report configured capabilities and worker readiness."""

import os
import shutil
from datetime import datetime, timezone

from fastapi import Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from cutsieve.infrastructure.media.local_inference import asr_model_config
from cutsieve.infrastructure.media.processing_units import asr_version

from cutsieve.infrastructure.persistence.edit_tables import jobs
from cutsieve.infrastructure.persistence.outbox_tables import outbox
from cutsieve.infrastructure.persistence.processing_tables import runs as processing
from cutsieve.infrastructure.persistence.workflow_tables import model_requests, runs as workflows
from cutsieve.infrastructure.providers.config import configured_model, model_supports_vision


def capabilities(env):
    config = configured_model(env)
    configured = config is not None
    asr_root, interpreter = asr_model_config(env)
    local_asr = asr_version(interpreter) if asr_root else None
    remote_asr = bool(env.get("CUTSIEVE_ASR_API_KEY"))
    services = {
        "OCR": env.get("CUTSIEVE_PADDLE_OCR_DIR") or env.get("CUTSIEVE_OCR_COMMAND") or shutil.which("tesseract"),
        "embedding": env.get("CUTSIEVE_SEARCH_ENCODER_URL") or env.get("CUTSIEVE_EMBEDDING_BASE_URL"),
        "reranker": env.get("CUTSIEVE_RERANKER_ENABLED", "").lower() == "true",
        "FFmpeg": env.get("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=env.get("CUTSIEVE_FFMPEG_DIR")),
    }
    return dict(
        general_model=config.model if config else "deepseek-v4-flash",
        resolved_model_version=config.resolved_model if config else "unverified",
        checked_at=datetime.now(timezone.utc).isoformat(),
        text=configured,
        vision=configured and model_supports_vision(env),
        native_tools=configured and env.get("CUTSIEVE_WORKFLOW_LIVE_MODEL", "1") != "0",
        structured_json=configured,
        dedicated_services=[
            dict(name="ASR", configured=bool(local_asr or remote_asr), ready=bool(local_asr or remote_asr),
                 verified=bool(local_asr), version=local_asr),
            *(dict(name=name, configured=bool(value), verified=False, version=None)
              for name, value in services.items()),
        ],
    )


def install_caps(app, workspace_user):
    @app.get("/v1/capabilities")
    def get_capabilities():
        return capabilities(os.environ)

    @app.get("/v1/readiness")
    def readiness(user=Depends(workspace_user)):
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
