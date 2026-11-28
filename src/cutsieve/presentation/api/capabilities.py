"""Report configured capabilities and worker readiness."""

import os
import shutil
from datetime import datetime, timezone
from subprocess import TimeoutExpired

from fastapi import Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from cutsieve.infrastructure.edit_media import media_binaries
from cutsieve.infrastructure.media.local_inference import asr_model_config, ocr_model_config
from cutsieve.infrastructure.media.process import command
from cutsieve.infrastructure.media.processing_units import asr_version, ocr_version

from cutsieve.infrastructure.persistence.edit_tables import jobs
from cutsieve.infrastructure.persistence.outbox_tables import outbox
from cutsieve.infrastructure.persistence.processing_tables import runs as processing
from cutsieve.infrastructure.persistence.workflow_tables import model_requests, runs as workflows
from cutsieve.infrastructure.providers.config import configured_model, model_supports_vision


def ffmpeg_service(env):
    binary = shutil.which(media_binaries(env)["ffmpeg"], path=env.get("PATH"))
    version = None
    if binary:
        try:
            output, _ = command([binary, "-version"], timeout=3)
            version = output.decode("utf-8", "replace").split(maxsplit=3)[2]
        except (OSError, ValueError, TimeoutExpired, IndexError):
            pass
    configured = binary or any(env.get(key) for key in (
        "CUTSIEVE_FFMPEG", "CUTSIEVE_FFMPEG_PATH", "CUTSIEVE_FFMPEG_DIR", "FFMPEG_DIR",
    ))
    return dict(name="FFmpeg", configured=bool(configured), ready=bool(version),
                verified=bool(version), version=version)


def embedding_service(env, encoder):
    versions = {}
    if encoder:
        try:
            versions = {space: model["version"] for space, model in encoder.spaces(timeout=3).items()}
        except (OSError, ValueError):
            pass
    return dict(
        name="embedding",
        configured=bool(encoder or env.get("CUTSIEVE_SEARCH_ENCODER_URL") or env.get("CUTSIEVE_EMBEDDING_BASE_URL")),
        ready=bool(versions), verified=bool(versions),
        version=versions.get("text") or versions.get("vision"), versions=versions,
    )


def capabilities(env, encoder=None):
    config = configured_model(env)
    configured = config is not None
    asr_root, interpreter = asr_model_config(env)
    local_asr = asr_version(interpreter) if asr_root else None
    remote_asr = bool(env.get("CUTSIEVE_ASR_API_KEY"))
    ocr_root, ocr_python = ocr_model_config(env)
    local_ocr = ocr_version(ocr_root, ocr_python) if ocr_root else None
    return dict(
        general_model=config.model if config else "deepseek-v4-flash",
        resolved_model_version=config.resolved_model if config else "unverified",
        checked_at=datetime.now(timezone.utc).isoformat(),
        text=configured,
        vision=configured and model_supports_vision(env),
        native_tools=configured and env.get("CUTSIEVE_WORKFLOW_LIVE_MODEL", "1") != "0",
        structured_json=configured,
        dedicated_services=[
            dict(name="ASR", configured=bool(asr_root or remote_asr), ready=bool(local_asr or remote_asr),
                 verified=bool(local_asr), version=local_asr),
            dict(name="OCR", configured=bool(ocr_root), ready=bool(local_ocr),
                 verified=bool(local_ocr), version=local_ocr),
            embedding_service(env, encoder),
            dict(name="reranker", configured=env.get("CUTSIEVE_RERANKER_ENABLED", "").lower() == "true",
                 verified=False, version=None),
            ffmpeg_service(env),
        ],
    )


def install_caps(app, workspace_user):
    @app.get("/v1/capabilities")
    def get_capabilities():
        return capabilities(os.environ, app.state.evidence_search.encoder)

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
