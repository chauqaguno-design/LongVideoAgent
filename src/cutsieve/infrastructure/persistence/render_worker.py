"""Claim revision-bound render jobs and publish verified artifacts transactionally."""
import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from cutsieve.application.edit_timeline import EditError, missing, uid
from cutsieve.application.render_formats import EXPORT_MIME
from cutsieve.infrastructure.edit_media import media_binaries
from cutsieve.infrastructure.media.process import check_running, MediaStopped
from .edit_repository import caption_font, one, owned, render_key
from .edit_tables import artifacts, jobs, revisions
from .work_outbox import emit

log = logging.getLogger(__name__)
LEASE_SECONDS = 90


def ready(now):
    return or_(jobs.c.status == "QUEUED", and_(jobs.c.status.in_(["RUNNING", "VALIDATING"]),
                                               jobs.c.lease_until < now))


def attempt(row):
    return and_(jobs.c.id == row.id, jobs.c.attempt == row.attempt,
                jobs.c.status.in_(["RUNNING", "VALIDATING"]))


class RenderWorker:
    def __init__(self, repo, signer):
        self.repo = repo
        self.root = Path(repo.sources.root) / "renders"
        self.signature = signer
        self.task = None
        self.stopping = asyncio.Event()

    async def start(self):
        self.stopping = asyncio.Event()
        self.task = asyncio.create_task(self.serve())

    async def stop(self):
        if self.task:
            self.stopping.set()
            await self.task
            self.task = None

    async def serve(self):
        while not self.stopping.is_set():
            try:
                row = await asyncio.to_thread(self.claim)
                if row is not None:
                    await self.run(row)
                    continue
            except SQLAlchemyError:
                log.exception("Render queue unavailable")
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=1)
            except TimeoutError:
                pass

    def claim(self):
        now = int(time.time())
        with self.repo.sessions() as db:
            key = db.execute(select(jobs.c.id).where(ready(now)).order_by(jobs.c.created_at).limit(1)).scalar()
        if not key:
            return None
        with self.repo.write() as db:
            count = db.execute(update(jobs).where(jobs.c.id == key, ready(now)).values(
                attempt=jobs.c.attempt + 1, lease_until=now+LEASE_SECONDS, status="RUNNING")).rowcount
            return one(db, select(jobs).where(jobs.c.id == key)) if count else None

    async def run(self, row):
        stopped = Event()
        task = asyncio.create_task(asyncio.to_thread(self.produce, row, stopped))
        next_renewal = time.monotonic() + LEASE_SECONDS / 3
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if done or stopped.is_set():
                    continue
                renew = time.monotonic() >= next_renewal
                if self.stopping.is_set() or not await asyncio.to_thread(self.owns_attempt, row, renew):
                    stopped.set()
                if renew:
                    next_renewal = time.monotonic() + LEASE_SECONDS / 3
            task.result()
        except MediaStopped:
            if self.stopping.is_set():
                with self.repo.write() as db:
                    db.execute(update(jobs).where(attempt(row)).values(status="QUEUED", lease_until=0))
        except SQLAlchemyError:
            # On an uncertain database connection, let the next valid lease reconcile the job.
            raise
        except Exception as exc:
            if isinstance(exc, EditError) and exc.code == "STALE_ATTEMPT":
                return
            log.warning("Render %s failed (%s)", row.id, type(exc).__name__)
            log.debug("Render worker diagnostic", exc_info=True)
            code = exc.code if isinstance(exc, EditError) else "RENDER_FAILED"
            message = str(exc) if isinstance(exc, EditError) else "渲染失败，请检查源媒体和编码工具"
            error = dict(code=code, message=message, retryable=True,
                         request_id=uid(), details={})
            with self.repo.write() as db:
                changed = db.execute(update(jobs).where(attempt(row)).values(status="FAILED", cache_slot=None,
                                     result=dict(row.result, error=error)))
                if changed.rowcount:
                    emit(db, uid(), row.workspace_id, row.id, "render.finished")
        finally:
            stopped.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    def owns_attempt(self, row, renew):
        with self.repo.sessions.begin() as db:
            if renew:
                return db.execute(update(jobs).where(attempt(row)).values(
                    lease_until=int(time.time())+LEASE_SECONDS)).rowcount == 1
            return db.scalar(select(jobs.c.id).where(attempt(row))) is not None

    def produce(self, row, stopped=None):
        from cutsieve.infrastructure.edit_media import render_timeline, renderer_version
        export_format = row.result.get("export_format", "mp4")
        if row.result.get("renderer_version", "cutsieve-ffmpeg-v1") != renderer_version(export_format):
            raise EditError(409, "RENDERER_CHANGED", "渲染器版本已变更，请为相同时间线重新生成")
        rev = self.repo.get(revisions, row.workspace_id, row.revision_id, lambda r: r.snapshot)
        sources = {mid: self.repo.sources.source_path(row.workspace_id, mid)
                   for mid in {c["media_revision_id"] for c in rev["clips"]}}
        tracks = {}
        if rev["caption_policy"] != "none":
            for clip in rev["clips"]:
                track = clip.get("caption_track_id")
                if track and track not in tracks:
                    tracks[track] = self.repo.resolve_track(row.workspace_id, clip["media_revision_id"], track)
        font = caption_font(rev)
        if font and render_key(row.workspace_id, rev, row.profile, font, export_format) != row.cache_key:
            raise ValueError("字幕字体已更改，请重新提交渲染")
        # An expired attempt can never overwrite a newer attempt's files.
        folder = self.root / row.id / str(row.attempt)
        folder.mkdir(parents=True, exist_ok=True)
        published = False
        try:
            output = folder / f"video.{export_format}"
            tools = media_binaries()
            def validating():
                with self.repo.write() as db:
                    if not db.execute(update(jobs).where(attempt(row)).values(status="VALIDATING")).rowcount:
                        raise EditError(409, "STALE_ATTEMPT", "该渲染任务已被取消或接管")
            audit = {}
            started = time.monotonic()
            manifest = render_timeline(rev, sources, output, job_id=row.id, profile=row.profile,
                                       caption_tracks=tracks, font_path=font, audit=audit, validating=validating,
                                       stopped=stopped, **tools)
            report = dict(render_job_id=row.id, revision_id=row.revision_id, duration_us=audit["structure"]["duration_us"],
                          expected_duration_us=manifest["total_duration_us"], full_decode=True, media=audit,
                          render_latency_ms=int((time.monotonic() - started) * 1000))
            for name, data in (("timeline", manifest), ("validation", report)):
                (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            files = [(row.profile, output), ("COMPILED_TIMELINE", folder/"timeline.json"),
                     ("VALIDATION_REPORT", folder/"validation.json")]
            records = []
            for kind, path in files:
                check_running(stopped)
                if kind == row.profile:
                    sha = audit["structure"]["output_sha256"]
                else:
                    with path.open("rb") as handle:
                        sha = hashlib.file_digest(handle, "sha256").hexdigest()
                relative = path.relative_to(self.root).as_posix()
                records.append(dict(id=uid(), workspace_id=row.workspace_id, revision_id=row.revision_id,
                               kind=kind, digest=sha, size=path.stat().st_size,
                               relative_path=relative))
            result = dict(row.result, progress=1.0, artifact_id=records[0]["id"],
                          compiled_manifest_artifact_id=records[1]["id"], validation_report_id=records[2]["id"])
            with self.repo.write() as db:
                check_running(stopped)
                self.repo.sources.require_readable(db, row.workspace_id, sources)
                if not db.execute(update(jobs).where(attempt(row)).values(status="SUCCEEDED", result=result)).rowcount:
                    return
                db.execute(insert(artifacts), records)
                emit(db,uid(),row.workspace_id,row.id,"render.finished")
            published = True
        finally:
            if not published:
                shutil.rmtree(folder, ignore_errors=True)

    def artifact_record(self, workspace, key):
        row = self.repo.get(artifacts, workspace, key, lambda r: r)
        rev = self.repo.get(revisions, workspace, row.revision_id, lambda r: r.snapshot)
        for mid in {c["media_revision_id"] for c in rev["clips"]}:
            self.repo.sources.media(workspace, mid)
        return row

    def artifact(self, workspace, key, request):
        row = self.artifact_record(workspace, key)
        expiry = int(time.time()) + 300
        url = request.url_for("edit_artifact_content", artifact_id=key).include_query_params(
            w=workspace, expires=expiry, sig=self.signature(workspace, key, expiry))
        return dict(artifact_id=key, workspace_id=workspace, kind=row.kind, sha256=row.digest,
                    size_bytes=row.size, mime_type=self.mime_type(row), media_revision_id=None,
                    timeline_revision_id=row.revision_id, url=str(url),
                    url_expires_at=datetime.fromtimestamp(expiry, timezone.utc).isoformat())

    def download(self, workspace, key, expiry, sig):
        if expiry <= time.time() or not hmac.compare_digest(self.signature(workspace, key, expiry).encode(), sig.encode()):
            raise missing()
        row = self.artifact_record(workspace, key)
        return self.materialize(row), self.mime_type(row)

    def materialize(self, row):
        path = self.root / row.relative_path
        if not path.is_file():
            raise missing()
        return path

    @staticmethod
    def mime_type(row):
        if row.kind in ("PREVIEW", "EXPORT"):
            return EXPORT_MIME[Path(row.relative_path).suffix[1:]]
        return "application/json"
