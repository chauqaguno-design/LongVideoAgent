"""A tombstone fences pending render and indexing work while shared source bytes stay usable."""

import os
import shutil
import subprocess
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select

from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES, jobs
from dovideo.infrastructure.persistence.outbox_tables import outbox
from dovideo.infrastructure.persistence.processing_tables import records, runs
from dovideo.infrastructure.persistence.processing_worker import ProcessingWorker
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base


@pytest.mark.real_media
def test_delete_fences_dependent_jobs_and_preserves_shared_source(tmp_path):
    exe = shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [exe, "-v", "error", "-f", "lavfi", "-i", "color=s=64x48:d=1", "-c:v", "libx264", str(path)],
        check=True,
    )
    sha = digest_file(path)
    engine = create_engine(f"sqlite:///{tmp_path/'data.db'}")
    Base.metadata.create_all(engine, tables=[projects, sources, runs, records, *EDIT_TABLES])
    store = EditSources(engine, tmp_path)
    worker = ProcessingWorker(store)
    cat = worker.catalog
    repo = EditRepository(store)
    scope = str(uuid4())
    project = store.create_project(scope, "shared")["project_id"]

    def copy():
        upload = store.create_upload(
            scope,
            project,
            dict(filename=path.name, size_bytes=path.stat().st_size, content_type="video/mp4"),
        )
        store.receive(upload, path, sha, upload.size)
        store.complete(
            scope, upload.upload, dict(idempotency_key="first", parts=[dict(part_number=1, etag=sha)])
        )
        return upload

    original, shared = copy(), copy()
    indexing = worker.enqueue(
        scope, original.id, dict(idempotency_key="index-before-delete", requested_capabilities=["ocr"])
    )
    edit = repo.create(
        scope,
        dict(
            project_id=project,
            title="test",
            output_profile=dict(width=64, height=48, fps_num=25, fps_den=1, audio_sample_rate=48000),
            idempotency_key="create",
        ),
    )
    clip = dict(
        media_revision_id=original.revision,
        source_start_us=0,
        source_end_us=500000,
        audio_policy="keep",
        evidence_ids=[],
    )
    revision = repo.patch(
        scope,
        edit["edit_project_id"],
        dict(
            expected_revision=0,
            reason="test",
            operations=[dict(op="append_clip", clip=clip)],
            idempotency_key="append",
        ),
    )
    render = repo.enqueue(
        scope, dict(revision_id=revision["revision_id"], profile="EXPORT", idempotency_key="render")
    )
    deleted = cat.delete_asset(scope, original.id)
    assert cat.delete_asset(scope, original.id)["processing_run_id"] == deleted["processing_run_id"]
    assert cat.get(scope, indexing["processing_run_id"])["status"] == "CANCELLED"
    with cat.sessions() as db:
        assert db.scalar(select(jobs.c.status).where(jobs.c.id == render["render_job_id"])) == "CANCELLED"
        events = set(db.execute(select(outbox.c.resource, outbox.c.kind)))
        assert (indexing["processing_run_id"], "processing.finished") in events
        assert (render["render_job_id"], "render.finished") in events
        assert isinstance(
            db.scalar(select(sources.c.probe).where(sources.c.id == original.id))["deleted_at"], int
        )
    with pytest.raises(HTTPException) as denied:
        store.source_path(scope, original.revision)
    assert denied.value.status_code == 404
    worker.perform(cat.claim())
    assert store.source_path(scope, shared.revision).is_file()
    cat.delete_asset(scope, shared.id)
    worker.perform(cat.claim())
    assert not store.blob(scope, sha).exists()
