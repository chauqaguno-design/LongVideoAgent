"""A durable processing job is its own delivery record; attempts carry a lease."""

import asyncio
from contextlib import suppress
import logging
import os
import shutil
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from cutsieve.application.media_sampling import PIPELINE
from cutsieve.infrastructure.media.processing_units import ProcessingUnits, local_models
from cutsieve.infrastructure.media.source_artifacts import SourceReader
from .processing_catalog import MediaCatalog, StaleAttempt, error, source_query
from .processing_tables import records
from .render_worker import media_binaries
from .source_tables import sources

log = logging.getLogger(__name__)


class ProcessingWorker:
    def __init__(self, sources_store):
        self.catalog = MediaCatalog(sources_store)
        self.interrupt = Event()
        self.task = None

    @staticmethod
    def config(body):
        version = body.get("pipeline_version", PIPELINE)
        if version != PIPELINE:
            raise HTTPException(422, "未知处理管线版本")
        return dict(
            body,
            pipeline_version=version,
            _local=local_models(),
            _frame_budget=max(0, min(65536, int(os.getenv("CUTSIEVE_MAX_MEDIA_FRAMES", "4096")))),
        )

    def enqueue(self, scope, asset_id, body):
        return self.catalog.enqueue(scope, asset_id, self.config(body))

    def register_upload(self, db, source):
        capabilities = ["asr", "ocr"]
        if os.getenv("CUTSIEVE_MEDIA_AUTO_VISUAL") == "1":
            capabilities.append("visual_observation")
        if os.getenv("CUTSIEVE_SEARCH_ENCODER_URL"):
            capabilities.append("image_embedding")
        config = self.config(
            dict(idempotency_key="initial-media-processing", requested_capabilities=capabilities)
        )
        self.catalog.register(db, source, config)

    async def start(self):
        self.stopping = asyncio.Event()
        self.task = asyncio.create_task(self.serve())

    async def stop(self):
        if self.task:
            self.stopping.set()
            self.interrupt.set()
            await self.task
            self.task = None

    async def serve(self):
        while not self.stopping.is_set():
            try:
                run = await asyncio.to_thread(self.catalog.claim)
                if run:
                    await self.execute(run)
                    continue
            except SQLAlchemyError:
                log.exception("Media processing queue unavailable")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=1)

    async def execute(self, run):
        self.interrupt.clear()
        job = asyncio.create_task(asyncio.to_thread(self.perform, run))
        while True:
            done, _ = await asyncio.wait({job}, timeout=1)
            if done:
                break
            if self.stopping.is_set() or not await asyncio.to_thread(self.catalog.heartbeat, run):
                self.interrupt.set()
        try:
            job.result()
        except StaleAttempt:
            return
        except Exception as exc:
            log.warning("Media job %s stopped (%s)", run.id, type(exc).__name__)
            log.debug("Media worker diagnostic", exc_info=True)
            try:
                if self.stopping.is_set():
                    self.catalog.commit(run, status="QUEUED", stage="RESUMING")
                else:
                    self.catalog.commit(
                        run,
                        status="FAILED",
                        stage="FAILED",
                        error=error("PROCESSING_FAILED", "处理失败，可重试并查看各阶段覆盖记录", True),
                    )
            except StaleAttempt:
                pass

    def perform(self, run):
        cat = self.catalog
        if run.kind == "DELETE":
            folder = cat.root / run.asset
            if folder.is_dir():
                shutil.rmtree(folder)
            with cat.write() as db:
                db.execute(
                    delete(records).where(
                        records.c.asset == run.asset, records.c.kind.not_in(("GENERATION", "VISUAL_CHARGE"))
                    )
                )
                row = db.execute(source_query(run.workspace, run.asset)).one()
                # Digests can be shared by other assets in the same workspace.
                candidates = db.execute(
                    select(sources.c.id, sources.c.digest, sources.c.probe)
                    .where(sources.c.workspace == run.workspace)
                    .with_for_update()
                ).all()
                shared = any(
                    c.id != row.id and c.digest == row.digest and not (c.probe or {}).get("deleted")
                    for c in candidates
                )
                if not shared and row.digest:
                    cat.sources.blob(run.workspace, row.digest).unlink(missing_ok=True)
            cat.commit(run, status="SUCCEEDED", stage="DELETED", completed=1, total=1)
            return
        with cat.sessions() as db:
            source = db.execute(source_query(run.workspace, run.asset)).one()
        if (source.probe or {}).get("deleted"):
            raise StaleAttempt()
        path = cat.sources.source_path(run.workspace, run.revision)
        reader = SourceReader(path, source.probe, media_binaries(), self.interrupt)
        cat.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cat.root) as temp:
            Path(temp, f".attempt-{run.id}-{run.attempt}").touch()
            ProcessingUnits(cat, run, source, reader, temp).run_all()
