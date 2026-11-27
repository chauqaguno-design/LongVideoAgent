"""Interrupt a real FFmpeg process through another database client or a graceful worker stop."""

import asyncio
import os
import shutil
import subprocess
from threading import Event
import time

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, select

from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import uid
from cutsieve.infrastructure.media.source_artifacts import digest_file
from cutsieve.infrastructure.persistence.edit_repository import EditRepository
from cutsieve.infrastructure.persistence.edit_tables import artifacts, EDIT_TABLES, jobs
from cutsieve.infrastructure.persistence.processing_tables import records, runs
from cutsieve.infrastructure.persistence.processing_worker import ProcessingWorker
from cutsieve.infrastructure.persistence.render_worker import RenderWorker
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base


@pytest.mark.real_media
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "phase"), [("cancel", "encode"), ("delete", "validate"), ("shutdown", "encode")]
)
async def test_real_render_process_stops_and_releases_its_attempt(
    tmp_path, monkeypatch, action, phase, record_property
):
    exe = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x64:r=10:d=12",
            "-c:v",
            "libx264",
            str(path),
        ],
        check=True,
    )
    url = f"sqlite:///{tmp_path/'render.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[projects, sources, runs, records, *EDIT_TABLES])
    store = EditSources(engine, tmp_path)
    local = EditRepository(store)
    peer = EditRepository(EditSources(create_engine(url), tmp_path))
    scope = uid()
    project = store.create_project(scope, "stop")["project_id"]
    upload = store.create_upload(
        scope, project, dict(filename=path.name, size_bytes=path.stat().st_size, content_type="video/mp4")
    )
    digest = digest_file(path)
    store.receive(upload, path, digest, upload.size)
    store.complete(
        scope, upload.upload, dict(idempotency_key="upload", parts=[dict(part_number=1, etag=digest)])
    )
    profile = dict(width=96, height=64, fps_num=25, fps_den=1, audio_sample_rate=48000)
    edit = local.create(
        scope, dict(project_id=project, title="stop", output_profile=profile, idempotency_key="create")
    )
    clip = dict(
        media_revision_id=upload.revision,
        source_start_us=0,
        source_end_us=12_000_000,
        audio_policy="keep",
        evidence_ids=[],
    )
    revision = local.patch(
        scope,
        edit["edit_project_id"],
        dict(
            expected_revision=0,
            idempotency_key="append",
            reason="test",
            operations=[dict(op="append_clip", clip=clip)],
        ),
    )["revision_id"]

    def enqueue(key):
        return local.enqueue(scope, dict(revision_id=revision, profile="EXPORT", idempotency_key=key))[
            "render_job_id"
        ]

    job = enqueue("first")
    worker = RenderWorker(local, None)
    started = Event()
    children = []
    real_popen = subprocess.Popen

    def monitored(args, *pos, **kwargs):
        target = ("graph.txt" in args) if phase == "encode" else ("-xerror" in args and "null" in args)
        slow = target and not started.is_set()
        if slow:
            # FFmpeg really reads the 12-second input at playback speed, leaving a known interruption window.
            args = list(args)
            args.insert(args.index("-i"), "-re")
        child = real_popen(args, *pos, **kwargs)
        if slow:
            children.append(child)
            started.set()
        return child

    monkeypatch.setattr(subprocess, "Popen", monitored)
    task = asyncio.create_task(worker.run(worker.claim()))
    assert await asyncio.to_thread(started.wait, 10)
    assert children[0].poll() is None
    beginning = time.monotonic()
    if action == "cancel":
        peer.cancel(scope, job, dict(idempotency_key="user-cancel", reason="test"))
    elif action == "delete":
        ProcessingWorker(peer.sources).catalog.delete_asset(scope, upload.id)
    else:
        worker.stopping.set()
    await asyncio.wait_for(task, 5)
    elapsed = time.monotonic() - beginning
    assert elapsed < 5 and children[0].poll() is not None
    record_property("stopped_seconds", round(elapsed, 3))
    with peer.sessions() as db:
        result = db.execute(select(jobs).where(jobs.c.id == job)).one()
        assert result.revision_id == revision and not db.execute(select(artifacts)).first()
    assert not (worker.root / job / "1").exists()
    if action == "delete":
        assert result.status == "CANCELLED"
        with pytest.raises(HTTPException):
            store.source_path(scope, upload.revision)
    else:
        if action == "shutdown":
            assert result.status == "QUEUED" and result.result["cancel_requested_at"] is None
        else:
            assert result.status == "CANCELLED" and result.result["cancel_requested_at"]
            job = enqueue("new-render")
        replacement = RenderWorker(peer, None)
        claim = replacement.claim()
        assert claim.id == job
        await asyncio.wait_for(replacement.run(claim), 12)
        with peer.sessions() as db:
            assert db.scalar(select(jobs.c.status).where(jobs.c.id == job)) == "SUCCEEDED"


@pytest.mark.real_media
@pytest.mark.asyncio
async def test_real_ffprobe_exits_on_stop(monkeypatch, record_property):
    from cutsieve.infrastructure.edit_media import _run
    from cutsieve.infrastructure.media.process import MediaStopped

    ffprobe = os.getenv("CUTSIEVE_FFPROBE") or shutil.which("ffprobe", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffprobe:
        pytest.skip("FFprobe required")
    started, stopped = Event(), Event()
    real_popen = subprocess.Popen
    children = []

    def track(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(subprocess, "Popen", track)
    args = [
        ffprobe,
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=64x64:r=8:d=10,realtime",
        "-show_frames",
    ]
    reading = asyncio.create_task(asyncio.to_thread(_run, args, stopped=stopped))
    assert await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(0.3)
    assert children[0].poll() is None
    begin = time.monotonic()
    stopped.set()
    with pytest.raises(MediaStopped):
        await asyncio.wait_for(reading, 2)
    assert children[0].poll() is not None
    record_property("stopped_seconds", round(time.monotonic() - begin, 3))
