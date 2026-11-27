"""Crash after encoding and SQL inserts but before publication commits."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import uid
from dovideo.infrastructure.persistence.edit_repository import EditRepository, job_view
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES, artifacts, jobs
from dovideo.infrastructure.persistence.render_worker import RenderWorker
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine


def repository(root):
    engine = create_sqlalchemy_engine("sqlite:///" + (root / "recovery.db").as_posix())
    return engine, EditRepository(EditSources(engine, root))


def crash_during_commit(root):
    engine, repo = repository(root)
    worker = RenderWorker(repo, None)
    row = worker.claim()
    normal_write = repo.write

    @contextmanager
    def interrupted_write():
        with normal_write() as db:
            yield db
            if db.scalar(select(func.count()).select_from(artifacts)):
                os._exit(77)

    repo.write = interrupted_write
    worker.produce(row)
    engine.dispose()


@pytest.mark.real_media
def test_restart_publishes_only_one_verified_artifact_set(tmp_path):
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("Real recovery integration needs FFmpeg")
    path = tmp_path / "bars.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=25:d=1",
                    "-c:v", "libx264", str(path)], check=True)
    engine, repo = repository(tmp_path)
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES])
    store = repo.sources
    scope = uid()
    project = store.create_project(scope, "recovery")["project_id"]
    size = path.stat().st_size
    row = store.create_upload(scope, project, dict(filename=path.name, size_bytes=size, content_type="video/mp4"))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    store.receive(row, path, sha, size)
    store.complete(scope, row.upload, dict(parts=[dict(part_number=1,etag=sha)],idempotency_key="upload-test"))
    profile = dict(width=64,height=48,fps_num=25,fps_den=1,audio_sample_rate=48000)
    edit = repo.create(scope,dict(project_id=project, title="recovery",output_profile=profile,idempotency_key="create-edit"))
    clip = dict(media_revision_id=row.revision,source_start_us=0,source_end_us=640000,audio_policy="keep",evidence_ids=[])
    rev = repo.patch(scope,edit["edit_project_id"],dict(expected_revision=0,reason="test",idempotency_key="append-test",
                    operations=[dict(op="append_clip",clip=clip)]))
    job = repo.enqueue(scope,dict(revision_id=rev["revision_id"],profile="EXPORT",idempotency_key="export-test"))
    killed = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path)], timeout=60)
    assert killed.returncode == 77
    jid = job["render_job_id"]
    assert (tmp_path/"renders"/jid/"1"/"video.mp4").stat().st_size > 0
    with repo.write() as db:
        assert db.scalar(select(func.count()).select_from(artifacts)) == 0
        assert db.scalar(select(jobs.c.status).where(jobs.c.id==jid)) == "VALIDATING"
        # Advance the lease boundary to simulate the recovery tick without a 90s wait.
        db.execute(jobs.update().where(jobs.c.id==jid).values(lease_until=0))
    worker = RenderWorker(repo,None)
    replacement = worker.claim()
    assert replacement.attempt == 2
    worker.produce(replacement)
    assert worker.claim() is None
    assert repo.get(jobs,scope,jid,job_view)["status"] == "SUCCEEDED"
    with repo.sessions() as db:
        published = db.execute(select(artifacts.c.kind,artifacts.c.relative_path)).all()
    assert {k for k,_ in published} == {"EXPORT","VALIDATION_REPORT","COMPILED_TIMELINE"}
    assert all(p.startswith(f"{jid}/2/") for _,p in published)
    assert len(published) == 3
    assert all((worker.root / p).is_file() for _, p in published)
    engine.dispose()


if __name__ == "__main__":
    crash_during_commit(Path(sys.argv[1]))
