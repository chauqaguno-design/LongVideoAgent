"""Stop a media worker after a durable frame checkpoint, then recover its real source."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import update
from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.processing_catalog import StaleAttempt
from dovideo.infrastructure.persistence.processing_tables import records, runs
from dovideo.infrastructure.persistence.processing_worker import ProcessingWorker
from dovideo.infrastructure.persistence.outbox_tables import inbox, outbox
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.workflow_tables import runs as workflow_runs
from dovideo.infrastructure.persistence.work_outbox import WorkOutbox
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine


def worker_at(root):
    engine = create_sqlalchemy_engine(f"sqlite:///{(root/'jobs.db').resolve().as_posix()}")
    Base.metadata.create_all(
        engine, tables=[projects, sources, runs, records, outbox, inbox, workflow_runs]
    )
    return ProcessingWorker(EditSources(engine, root))


def crash(root, point):
    worker = worker_at(root)
    run = worker.catalog.claim()
    commit = worker.catalog.commit

    def checkpoint(run, items=(), **state):
        frame = any(r["kind"] == "CHECKPOINT" and r["unit"] == "frames:0:0" for r in items)
        if frame and point == "before-frame-commit":
            os._exit(77)
        commit(run, items, **state)
        if frame and point == "after-frame-commit":
            os._exit(77)

    worker.catalog.commit = checkpoint
    worker.perform(run)
    if point == "after-publish":
        os._exit(77)


@pytest.mark.real_media
@pytest.mark.parametrize("point", ["before-frame-commit", "after-frame-commit"])
def test_committed_frame_survives_crash_and_late_output_is_fenced(tmp_path, monkeypatch, point):
    exe = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    monkeypatch.delenv("DOVIDEO_PADDLE_OCR_DIR", raising=False)
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=64x48:r=8:d=2",
            "-c:v",
            "libx264",
            str(path),
        ],
        check=True,
    )
    worker = worker_at(tmp_path)
    cat = worker.catalog
    source = cat.sources
    scope = str(uuid4())
    project = source.create_project(scope, "崩溃恢复")["project_id"]
    upload = source.create_upload(
        scope,
        project,
        dict(filename="sample.mp4", size_bytes=path.stat().st_size, content_type="video/mp4"),
    )
    digest = digest_file(path)
    source.receive(upload, path, digest, upload.size)
    source.complete(
        scope,
        upload.upload,
        dict(idempotency_key="sample-initial", parts=[dict(part_number=1, etag=digest)]),
    )

    def enqueue(key):
        return worker.enqueue(scope, upload.id, dict(idempotency_key=key, requested_capabilities=["ocr"]))

    initial = enqueue("run-first")
    assert enqueue("run-first") == initial
    child = subprocess.run([sys.executable, __file__, str(tmp_path), point])
    assert child.returncode == 77
    with cat.write() as db:
        old = db.execute(runs.select().where(runs.c.id == initial["processing_run_id"])).one()
        db.execute(update(runs).where(runs.c.id == old.id).values(lease_end=0))
    assert old.attempt == 1 and list((cat.root / upload.id / "files").glob("*.jpg"))
    assert any(r.unit == "frames:0:0" for r in cat.entries(old, "CHECKPOINT")) == (
        point == "after-frame-commit"
    )
    retry = cat.claim()
    assert retry.id == old.id and retry.attempt == 2
    with pytest.raises(StaleAttempt):
        cat.commit(old, stage="LATE")
    worker.perform(retry)
    first_id, first_rows = cat.search_scope(scope, project)
    assert len([r for r in first_rows if r["source"] == "sampled-frame"]) == 4
    assert cat.get(scope, retry.id)["index_generation_id"] == first_id
    enqueue("run-two")
    enqueue("run-three")
    second = cat.claim()
    third = cat.claim()
    worker.perform(second)
    worker.perform(third)
    assert cat.get(scope, third.id)["error"]["code"] == "GENERATION_CONFLICT"
    new_id, new_rows = cat.search_scope(scope, project)
    assert first_id != new_id and len(first_rows) == len(new_rows)
    assert [r["evidence_id"] for r in cat.search_scope(scope, project, generation_id=first_id)[1]] == [
        r["evidence_id"] for r in first_rows
    ]
    enqueue("run-last")
    on_way = cat.claim()
    assert cat.cancel(scope, on_way.id)["status"] == "CANCELLED"
    with pytest.raises(StaleAttempt):
        cat.commit(on_way, stage="LATE")
    assert cat.search_scope(scope, project)[0] == new_id
    if point == "after-frame-commit":
        last = enqueue("publish-before-delivery")["processing_run_id"]
        assert subprocess.run([sys.executable, __file__, str(tmp_path), "after-publish"]).returncode == 77
        assert cat.claim() is None and cat.get(scope, last)["index_generation_id"]
        with cat.sessions() as db:
            event = db.execute(outbox.select().where(outbox.c.resource == last)).one()
            assert event.status == "PENDING"
        dispatch = WorkOutbox(EditRepository(source))
        while dispatch.dispatch_one():
            pass
        assert not dispatch.receive(event.id)
        with cat.sessions() as db:
            assert db.execute(outbox.select().where(outbox.c.id == event.id)).one().status == "SENT"


if __name__ == "__main__":
    crash(Path(sys.argv[1]), sys.argv[2])
