"""Exercise real SQL commits, same-key replay and competing writers."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.infrastructure.persistence.edit_repository import EditRepository, project_view
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES, events, jobs, projects, revisions
from cutsieve.infrastructure.persistence.source_tables import projects as source_projects, sources as source_rows
from cutsieve.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine


PROFILE = dict(width=320, height=240, fps_num=24, fps_den=1, audio_sample_rate=48000)


@pytest.fixture
def setup(tmp_path):
    engine = create_sqlalchemy_engine(f"sqlite:///{tmp_path}/edit.db")
    Base.metadata.create_all(engine, tables=[source_projects, source_rows, *EDIT_TABLES])
    store = EditSources(engine, tmp_path)
    workspace = uid()
    parent_id = store.create_project(workspace, "剪辑测试")["project_id"]
    upload = store.create_upload(workspace, parent_id, dict(filename="fixture.mp4", size_bytes=20, content_type="video/mp4"))
    probe = dict(duration_us=4000000, streams=[], source_origin=dict(num=0, den=1), width=320, height=240)
    with store.sessions.begin() as db:
        db.execute(source_rows.update().where(source_rows.c.upload == upload.upload)
                   .values(digest="0"*64, receipt="1"*64, probe=probe))
    repo = EditRepository(store)
    project = repo.create(workspace, dict(project_id=parent_id, title="测试", output_profile=PROFILE,
                                         idempotency_key="edit-test-setup"))
    clip = dict(media_revision_id=upload.revision, source_start_us=0, source_end_us=1000000,
                evidence_ids=[], audio_policy="keep")
    yield repo, workspace, project["edit_project_id"], clip
    engine.dispose()


def apply(repo, scope, edit_id, number, key, *operations):
    return repo.patch(scope, edit_id, dict(expected_revision=number, idempotency_key=key,
                                           operations=list(operations), reason="test"))


def test_history_replace_reorder_restore_and_atomic_rollback(setup):
    repo, scope, edit, clip = setup
    initial = repo.get(projects, scope, edit, project_view)["head_revision_id"]
    append = dict(op="append_clip", clip=clip)
    first = apply(repo, scope, edit, 0, "add-clips", append, append)
    c1, c2 = [v["clip_id"] for v in first["clip_id_assignments"]]
    second = apply(repo, scope, edit, 1, "move-clips", dict(op="replace_clip", clip_id=c1,
                   clip=dict(clip, audio_policy="mute")), dict(op="reorder_clips", clip_ids=[c2, c1]))
    rev = repo.get(revisions, scope, second["revision_id"], lambda r: r.snapshot)
    assert [c["clip_id"] for c in rev["clips"]] == [c2, c1]
    assert rev["clips"][1]["audio_policy"] == "mute"
    with pytest.raises(EditError):
        apply(repo, scope, edit, 2, "bad-patch", dict(op="remove_clip", clip_id=c1), dict(op="remove_clip", clip_id=c1))
    with pytest.raises(EditError):
        apply(repo, scope, edit, 2, "bad-range", dict(op="append_clip", clip=dict(clip, source_end_us=4000001)))
    with pytest.raises(EditError):
        apply(repo, scope, edit, 2, "bad-crop", dict(op="append_clip", clip=dict(clip, crop=dict(x=300,y=0,width=60,height=40))))
    restored = apply(repo, scope, edit, 2, "restore-root", dict(op="restore_revision", source_revision_id=initial))
    current = repo.get(revisions, scope, restored["revision_id"], lambda r: r.snapshot)
    assert current["revision_no"] == 3 and current["clips"] == []
    first_page = repo.page(revisions, scope, lambda r: r.number, dict(edit_id=edit), None, 2)
    assert first_page == dict(items=[3,2], next_cursor="2")
    assert repo.page(revisions, scope, lambda r: r.number, dict(edit_id=edit), "2", 2)["items"] == [1,0]
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(events).where(events.c.edit_id==edit)) == 3


@pytest.mark.parametrize("shared_key", [True, False])
def test_competing_transactions(setup, shared_key):
    repo, scope, edit, clip = setup
    start = Barrier(2)
    def submit(i):
        start.wait()
        try:
            return apply(repo, scope, edit, 0, "key-shared" if shared_key else f"edit-key-{i}",
                         dict(op="append_clip", clip=clip))
        except EditError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit, [1,2]))
    if shared_key:
        assert sorted(r["replayed"] for r in replies) == [False,True]
        assert replies[0]["revision_id"] == replies[1]["revision_id"]
        with pytest.raises(EditError, match="幂等"):
            apply(repo, scope, edit, 1, "key-shared", dict(op="append_clip", clip=clip))
    else:
        assert replies.count("REVISION_CONFLICT") == 1
    assert repo.get(projects, scope, edit, project_view)["head_revision"] == 1


def test_empty_timeline_does_not_enqueue(setup):
    repo, scope, edit, clip = setup
    empty = repo.get(projects, scope, edit, project_view)["head_revision_id"]
    tiny = dict(clip, source_start_us=0, source_end_us=1)
    short = apply(repo, scope, edit, 0, "one-microsecond", dict(op="append_clip", clip=tiny))
    for revision_id in (empty, short["revision_id"]):
        with pytest.raises(EditError) as rejected:
            repo.enqueue(scope, dict(revision_id=revision_id, profile="EXPORT", idempotency_key="zero-frames"))
        assert (rejected.value.status, rejected.value.code) == (422, "EMPTY_TIMELINE")
    with repo.sessions() as db:
        assert db.execute(jobs.select().where(jobs.c.edit_id == edit)).first() is None


def test_job_replay_and_cancel_fences_expired_attempt(setup):
    from cutsieve.infrastructure.persistence.render_worker import RenderWorker
    repo, scope, edit, clip = setup
    rev = apply(repo, scope, edit, 0, "add-clips", dict(op="append_clip", clip=clip))
    request = dict(revision_id=rev["revision_id"], profile="EXPORT", idempotency_key="render-0001")
    job = repo.enqueue(scope, request)
    assert repo.enqueue(scope, dict(request, idempotency_key="render-0002")) == job
    worker = RenderWorker(repo, None)
    first = worker.claim()
    assert worker.claim() is None
    with repo.write() as db:
        db.execute(jobs.update().where(jobs.c.id==first.id).values(lease_until=0))
    retry = worker.claim()
    assert retry.id == first.id and retry.attempt > first.attempt
    done = repo.cancel(scope, first.id, dict(idempotency_key="cancel-0001"))
    assert done["status"] == "CANCELLED" and worker.claim() is None
    assert repo.enqueue(scope, request)["status"] == "CANCELLED"
    assert repo.enqueue(scope, dict(request,idempotency_key="render-0003"))["render_job_id"] != first.id
    with pytest.raises(EditError) as denied:
        repo.cancel(uid(), first.id, dict(idempotency_key="bad-cancel"))
    assert denied.value.status == 404
